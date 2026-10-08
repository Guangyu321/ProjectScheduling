"""Compose experiment operations into executable workflows; no hardware drivers."""
import copy
import math
from pathlib import Path
import re
import tempfile

from .validation import require, index, validate

LABELS = {
    'identity.scan': ('扫码', 'SIM_SCAN_V1', 1),
    'liquid.transfer': ('移液', 'SIM_PIPETTE_V1', 8),
    'plate.seal': ('封膜', 'SIM_SEAL_V1', 4),
    'plate.peel': ('撕膜', 'SIM_PEEL_V1', 4),
    'plate.shake': ('振荡', 'SIM_SHAKE_V1', 6),
    'plate.centrifuge': ('离心（动作模拟）', 'SIM_CENTRIFUGE_V1', 8),
    'fluorescence.measure': ('荧光检测', 'SIM_MEASURE_V1', 5),
    'storage.store': ('入库确认', 'SIM_STORE_V1', 1),
    'storage.retrieve': ('出库确认', 'SIM_RETRIEVE_V1', 1),
    'stack.present': ('耗材出板', 'SIM_PRESENT_V1', 1),
}


def operation_catalog(cell):
    capabilities = sorted({cap for d in cell['devices'] for cap in d['capabilities']} - {'plate.transport'})
    result = []
    for cap in capabilities:
        label, method, duration = LABELS.get(cap, (cap, 'SIM_METHOD_V1', 1))
        result.append({'capability': cap, 'label': label, 'method_id': method,
                       'estimated_duration_s': duration, 'timeout_s': duration + 10,
                       'devices': [d['id'] for d in cell['devices'] if cap in d['capabilities']]})
    return result


def default_recipe(cell, flow):
    first = flow['plates'][0]['id']
    return {
        'id': 'builtin_standard', 'name': '标准双板实验', 'version': 1,
        'execution_mode': 'parallel', 'plates': copy.deepcopy(flow['plates']),
        'operations': [
            {'id': s['id'].split('.', 1)[1], 'capability': s['capability'],
             'method_id': s['parameters'].get('method_id', 'SIM_SCAN_V1'),
             'estimated_duration_s': s['estimated_duration_s'], 'timeout_s': s['timeout_s']}
            for s in flow['steps'] if s['plate_id'] == first and s['kind'] == 'process'
        ],
    }


def _identifier(value, label):
    require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,80}', value),
            f'{label}仅允许 1–80 位字母、数字、下划线或连字符')


def compile_recipe(cell, recipe):
    """Bind capabilities and generate transports without mutating input or live state."""
    try:
        return _compile(cell, recipe)
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f'流程配置格式错误：{exc}') from exc


def _compile(cell, recipe):
    require(isinstance(recipe, dict), '流程必须是对象')
    require(not set(recipe) - {'id', 'name', 'version', 'execution_mode', 'plates', 'operations'}, '流程包含不支持的字段')
    _identifier(recipe.get('id'), '流程标识')
    require(isinstance(recipe.get('name'), str) and 0 < len(recipe['name'].strip()) <= 100, '请填写流程名称（最多 100 字）')
    require(type(recipe.get('version')) is int and recipe['version'] > 0, '流程版本必须是正整数')
    mode = recipe.get('execution_mode', 'serial')
    require(mode in ('serial', 'parallel'), '执行模式必须是 serial 或 parallel')
    plates = index(recipe.get('plates'), '样品板')
    operations = index(recipe.get('operations'), '实验步骤')
    require(len(plates) <= 8 and len(operations) <= 50, '每个模板最多 8 块板、50 个实验步骤')
    devices = index(cell['devices'], 'devices'); locations = index(cell['locations'], 'locations')
    reachable = set(cell['transport']['reachable_locations'])
    for plate in plates.values():
        _identifier(plate['id'], '板标识')
        require(not set(plate) - {'id', 'barcode', 'type', 'initial_location', 'final_location'}, '样品板包含不支持的字段')
        for field in ('initial_location', 'final_location'):
            require(plate[field] in locations, '样品板起止位置不存在')
    for op in operations.values():
        _identifier(op['id'], '步骤标识')
        require(op['id'] != 'finish' and not op['id'].startswith('move_to_'), '步骤标识使用了系统保留前缀')
        require(not set(op) - {'id', 'capability', 'device_id', 'location', 'method_id', 'estimated_duration_s', 'timeout_s'},
                '实验步骤包含不支持的字段')
        require(isinstance(op.get('capability'), str) and op['capability'] != 'plate.transport', '请选择实验能力；转运由系统生成')
        for field in ('device_id', 'location'):
            require(isinstance(op.get(field, ''), str), f'{field} 必须是字符串')
        for field in ('estimated_duration_s', 'timeout_s'):
            number = op.get(field)
            require(type(number) in (int, float) and math.isfinite(number) and 0 < number <= 86400,
                    '时长必须大于 0 且不超过 86400 秒')
        require(op['timeout_s'] >= op['estimated_duration_s'], '超时时间不能小于预计时长')
        if op['capability'] != 'identity.scan':
            require(isinstance(op.get('method_id'), str) and 0 < len(op['method_id'].strip()) <= 200, '请填写方法标识（最多 200 字）')

    steps = []; previous_plate_end = None
    for plate in plates.values():
        current = plate['initial_location']
        previous = previous_plate_end if mode == 'serial' else None

        def append_step(suffix, **fields):
            nonlocal previous
            step = {'id': f"{plate['id']}.{suffix}", 'plate_id': plate['id'],
                    'depends_on': [previous] if previous else [], **fields}
            steps.append(step); previous = step['id']

        def move(target, suffix):
            nonlocal current
            if current != target:
                append_step('move_to_' + suffix, kind='transport', capability='plate.transport',
                            device_id=cell['transport']['robot_id'], source=current, target=target,
                            parameters={}, estimated_duration_s=2, timeout_s=10)
                current = target

        for op in operations.values():
            # Prefer a usable current slot, then the requested final storage slot.
            candidates = [p for p in locations.values()
                          if p['device_id'] in devices
                          and op['capability'] in devices[p['device_id']]['capabilities']
                          and devices[p['device_id']]['state'] == 'idle'
                          and plate['type'] in devices[p['device_id']]['plate_types']
                          and plate['type'] in p['plate_types']
                          and (p['id'] == current or p['id'] in reachable)
                          and (not op.get('device_id') or p['device_id'] == op['device_id'])
                          and (not op.get('location') or p['id'] == op['location'])]
            require(candidates, f"步骤 {op['id']}：没有满足 {op['capability']}、设备和板位约束的可用设备")
            preferred = plate['final_location'] if op['capability'] == 'storage.store' else current
            candidates.sort(key=lambda p: (p['id'] != preferred, p['id'] != current, p['id']))
            target = candidates[0]
            move(target['id'], op['id'])
            parameters = ({'expected_barcode': plate['barcode']} if op['capability'] == 'identity.scan'
                          else {'method_id': op['method_id']})
            append_step(op['id'], kind='process', capability=op['capability'], device_id=target['device_id'],
                        location=target['id'], parameters=parameters,
                        estimated_duration_s=op['estimated_duration_s'], timeout_s=op['timeout_s'])
        move(plate['final_location'], 'finish')
        previous_plate_end = previous
    workflow = {'schema_version': 1, 'simulation_only': True, 'definition_id': recipe['id'],
                'name': recipe['name'], 'version': recipe['version'],
                'plates': copy.deepcopy(recipe['plates']), 'steps': steps,
                'recipe': copy.deepcopy(recipe)}
    validate(cell, workflow)
    return workflow


def preview_recipe(cell, recipe):
    """Exercise the actual scheduler against isolated, fault-free simulated devices."""
    from .engine import Engine
    from .simulator import Simulator
    flow = compile_recipe(cell, recipe)
    definitions = {s['id']: s for s in flow['steps']}
    with tempfile.TemporaryDirectory(prefix='labflow-preview-') as directory:
        simulator = Simulator(Path(directory)/'devices.sqlite')
        engine = None
        try:
            engine = Engine(cell, Path(directory)/'runtime.sqlite', simulator)
            engine.submit(flow, 'preview')
            now = 0.0
            for _ in range(len(flow['steps']) + 1):
                engine.tick(now)
                commands = engine.store.db.execute('SELECT * FROM commands').fetchall()
                if engine.store.db.execute('SELECT state FROM runs').fetchone()[0] == 'succeeded':
                    timeline = [{'step_id': c['step_id'], 'device_id': definitions[c['step_id']]['device_id'],
                                 'plate_id': definitions[c['step_id']]['plate_id'],
                                 'started': c['started'], 'ended': c['ended']} for c in commands]
                    return {'workflow': flow, 'timeline': timeline,
                            'summary': {'plates': len(flow['plates']), 'operations': len(recipe['operations']),
                                        'steps': len(flow['steps']), 'transports': sum(s['kind'] == 'transport' for s in flow['steps']),
                                        'duration_s': now}}
                active = [c for c in commands if c['state'] == 'running']
                require(active, '流程预演发生资源或板位阻塞；请调整起止位置、步骤顺序，或改为逐板执行')
                # Skip idle ticks but preserve the console's one-second dispatch cadence.
                now = max(now + 1, math.ceil(min(c['started'] + definitions[c['step_id']]['estimated_duration_s'] for c in active)))
            raise ValueError('流程预演未能完成')
        finally:
            if engine: engine.close()
            simulator.close()
