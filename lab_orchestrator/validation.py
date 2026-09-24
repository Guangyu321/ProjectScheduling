"""Configuration validation for fixed-route, capacity-one simulation."""
import math


def require(condition, message):
    if not condition:
        raise ValueError(message)


def index(items, name):
    require(isinstance(items,list) and bool(items), f'{name} must be a non-empty list')
    result={}
    for item in items:
        require(isinstance(item,dict), f'{name} entries must be objects')
        key=item.get('id')
        require(isinstance(key,str) and bool(key.strip()), f'{name}: missing id')
        require(key not in result, f'{name}: duplicate id {key}')
        result[key]=item
    return result


def resources_for(cell,step):
    """Compute all resources required for an atomic reservation."""
    locations={p['id']:p for p in cell['locations']}
    resources={step['device_id']+'.processing','plate:'+step['plate_id']}
    if step['kind']=='transport':
        resources.update(cell['transport']['shared_resources'])
        resources.add('location:'+step['device_id']+'.slot1')
        positions=[step['source'],step['target']]
    else:
        positions=[step['location']]
    for position in positions:
        resources.add('location:'+position)
        owner=locations[position]['device_id']
        if owner:
            resources.update([owner+'.port',owner+'.processing'])
        else:
            resources.add('buffer.port')
    return sorted(resources)


def validate(cell,flow):
    """Reject unsupported definitions before creating any execution state."""
    try:
        _validate(cell,flow)
    except (KeyError,TypeError,AttributeError) as exc:
        raise ValueError(f'Malformed configuration: {exc}') from exc


def _validate(cell,flow):
    for name,obj in [('workcell',cell),('workflow',flow)]:
        require(isinstance(obj,dict), f'{name} must be an object')
        require(obj.get('schema_version')==1, f'{name}: unsupported schema')
        require(obj.get('simulation_only') is True, 'Only simulation_only=true supported')
    require(isinstance(cell.get('station_id'),str) and cell['station_id'].strip(), 'station_id required')
    require(isinstance(flow.get('definition_id'),str) and flow['definition_id'].strip(), 'definition_id required')
    require(type(flow.get('version')) is int and flow['version']>0, 'Positive workflow version required')
    devices=index(cell['devices'],'devices'); locations=index(cell['locations'],'locations')
    resources=index(cell['resources'],'resources'); plates=index(flow['plates'],'plates')
    steps=index(flow['steps'],'steps')
    for device in devices.values():
        require(device['driver']=='simulated', 'Real drivers are not supported')
        require(device['state'] in ('idle','offline','fault'), 'Unsupported device state')
        require(isinstance(device['capabilities'],list) and device['capabilities'], 'Capabilities required')
        require(isinstance(device['plate_types'],list) and device['plate_types'], 'Plate types required')
    for resource in resources.values():
        require(type(resource['capacity']) is int and resource['capacity']==1, 'Only capacity one supported')
    for position in locations.values():
        require(position['capacity']==1, 'Only single-slot locations supported')
        require(position['device_id'] is None or position['device_id'] in devices, 'Location owner not found')
    robot=cell['transport']['robot_id']
    require(robot in devices and robot+'.slot1' in locations, 'Robot and gripper position required')
    reachable=cell['transport']['reachable_locations']
    require(isinstance(reachable,list) and set(reachable)<=locations.keys(), 'Unknown reachable location')
    require(all(r in resources for r in cell['transport']['shared_resources']), 'Unknown transport resource')
    occupied=set(); barcodes=set()
    for plate in plates.values():
        require(isinstance(plate['barcode'],str) and plate['barcode'].strip(), 'Barcode required')
        require(plate['barcode'] not in barcodes, 'Duplicate barcode'); barcodes.add(plate['barcode'])
        for key in ('initial_location','final_location'):
            require(plate[key] in locations, 'Unknown plate location')
            require(plate['type'] in locations[plate[key]]['plate_types'], 'Unsupported plate at location')
        require(plate['initial_location'] not in occupied, 'Initial location already occupied')
        occupied.add(plate['initial_location'])
    last={}; current={p:plate['initial_location'] for p,plate in plates.items()}
    for step in steps.values():
        plate_id=step['plate_id']; device_id=step['device_id']
        require(plate_id in plates and device_id in devices, 'Unknown plate/device')
        require(step['kind'] in ('transport','process'), 'Unsupported step kind')
        require(step['capability'] in devices[device_id]['capabilities'], 'Capability mismatch')
        require(plates[plate_id]['type'] in devices[device_id]['plate_types'], 'Device plate type mismatch')
        expected=[] if plate_id not in last else [last[plate_id]]
        require(step['depends_on']==expected, 'Expected an ordered linear per-plate dependency chain')
        for field in ('estimated_duration_s','timeout_s'):
            n=step[field]
            require(type(n) in (int,float) and math.isfinite(n) and n>0, 'Duration must be positive and finite')
        require(step['timeout_s']>=step['estimated_duration_s'], 'Timeout shorter than simulated duration')
        require(isinstance(step['parameters'],dict), 'Parameters must be an object')
        if step['kind']=='transport':
            require(device_id==robot and step['capability']=='plate.transport', 'Wrong transport robot')
            source,target=step['source'],step['target']
            require(source in reachable and target in reachable and source!=target, 'Unreachable transport')
            require(source==current[plate_id], 'Transport source breaks the plate route')
            require(plates[plate_id]['type'] in locations[target]['plate_types'], 'Target plate type mismatch')
            require(step['parameters']=={}, 'Transport parameters unsupported in v0.1')
            current[plate_id]=target
        else:
            location=step['location']
            require(location==current[plate_id], 'Process location breaks the plate route')
            require(locations[location]['device_id']==device_id, 'Process device does not own location')
            param='expected_barcode' if step['capability']=='identity.scan' else 'method_id'
            require(set(step['parameters'])=={param}, 'Unsupported simulation parameters')
            require(isinstance(step['parameters'][param],str) and step['parameters'][param].strip(), 'Parameter required')
            if param=='expected_barcode':
                require(step['parameters'][param]==plates[plate_id]['barcode'], 'Expected barcode mismatch')
        known=set(resources)|{'location:'+p for p in locations}|{'plate:'+p for p in plates}
        require(set(resources_for(cell,step))<=known, 'Missing required resource')
        last[plate_id]=step['id']
    require(set(last)==set(plates), 'Every plate needs a route')
    require(all(current[p]==plate['final_location'] for p,plate in plates.items()), 'Incorrect final location')
