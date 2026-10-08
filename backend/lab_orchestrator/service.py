"""A single owning thread serializes all HTTP/user and scheduler operations."""
from concurrent.futures import Future
from datetime import datetime,timezone
import copy
import json
from pathlib import Path
from queue import Queue,Empty
import threading
import time
from uuid import uuid4
from .controls import ConflictError
from .drivers import DeviceRouter,default_registry
from .engine import Engine
from .storage import Store,digest,encode
from .validation import validate
from .recipes import operation_catalog,default_recipe,preview_recipe

ROOT=Path(__file__).resolve().parents[1]
SCENARIOS={'normal':{},'lost_ack':{'plate_01.pipette':'lost_ack'},'unknown':{'plate_01.move_to_scan':'unknown'}}


class BatchNotFoundError(ValueError):
    """A queued control targets a batch other than the active one."""


class PanelService:
    def __init__(self,directory,auto_interval=0.3):
        if auto_interval is not None and (type(auto_interval) not in (int,float) or not 0.05<=auto_interval<=60):
            raise ValueError('auto_interval must be between 0.05 and 60 seconds')
        self.directory=Path(directory).resolve()
        self.auto_interval=auto_interval
        self.queue=Queue();self.ready=Future();self._closed=False;self._error=None
        self.thread=threading.Thread(target=self._worker,name='lab-controller',daemon=True)
        self.thread.start()
        try:self.ready.result(timeout=20)
        except BaseException:self._closed=True;raise

    def call(self,action,**payload):
        if self._closed:raise RuntimeError('Control service is closed')
        future=Future();self.queue.put((action,payload,future))
        # Accepted operations finish exactly once; HTTP disconnects do not cancel them.
        return future.result(timeout=30)

    def _worker(self):
        self.engine=None;self.router=None;self.panel=None;self.current=None
        try:
            self.cell=json.loads((ROOT/'examples/workcell.json').read_text(encoding='utf-8'))
            self.flow=json.loads((ROOT/'examples/workflow.json').read_text(encoding='utf-8'))
            self.registry=default_registry()
            self.panel=Store(self.directory/'panel.sqlite')
            self.current=self.panel.meta('current')
            if self.current:
                self._open(self.current)
                self.engine.recover()
                state=self.engine.snapshot()
                if state['state'] not in ('succeeded','cancelled') and state['control_intent']!='cancel':
                    self.engine.pause(actor='system:service-restart')
            self.ready.set_result(True)
            deadline=time.monotonic()+(self.auto_interval or 3600)
            while True:
                timeout=max(0,deadline-time.monotonic()) if self.auto_interval else None
                try:item=self.queue.get(timeout=timeout)
                except Empty:item=None
                if item:
                    action,payload,future=item
                    if action=='close':
                        future.set_result(None);break
                    try:future.set_result(self._dispatch(action,payload))
                    except Exception as exc:future.set_exception(exc)
                if self.auto_interval and time.monotonic()>=deadline:
                    if self.engine and not self._error:
                        try:
                            state=self.engine.snapshot()['state']
                            if state in ('running','pausing','cancelling'):
                                self.engine.tick(self.engine.now+1)
                                self._save_summary()
                        except Exception as exc:
                            self._error=f'{type(exc).__name__}: {exc}'
                    deadline=time.monotonic()+self.auto_interval
        except BaseException as exc:
            if not self.ready.done():self.ready.set_exception(exc)
            self._error=f'{type(exc).__name__}: {exc}'
        finally:
            if self.engine:self.engine.close()
            if self.router:self.router.close()
            if self.panel:self.panel.close()

    def _open(self,record):
        path=self.directory/'batches'/record['session_id']
        router=DeviceRouter(self.cell,path/'devices',self.registry,faults=record.get('faults',SCENARIOS[record['scenario']]))
        try:engine=Engine(self.cell,path/'runtime.sqlite',router)
        except BaseException:router.close();raise
        self.router=router;self.engine=engine

    def _save_summary(self):
        if not self.engine:return
        state=self.engine.snapshot()
        summary={**self.current,'state':state['state'],'sim_time':state['sim_time'],
                 'completed':sum(s['state']=='succeeded' for s in state['steps']),'total':len(state['steps'])}
        with self.panel.transaction():
            self.panel.set_meta('summary:'+self.current['session_id'],summary)

    def _state(self):
        state=self.engine.snapshot() if self.engine else {
            'state':'empty','run_id':None,'version':0,'sim_time':0,'steps':[],'commands':[],'events':[],
            'locks':[],'alarms':[],'recoveries':[],'positions':{},'certainty':{},'control_intent':'pause'}
        held={r['resource'] for r in state['locks']}
        commands={c['id']:c for c in state['commands']}
        unknown_resources={r['resource'] for r in state['locks'] if commands[r['command_id']]['state']=='blocked'}
        devices=[]
        for d in self.cell['devices']:
            status=d['state']
            if d['id']+'.processing' in held:status='busy'
            if d['id']+'.processing' in unknown_resources:status='unknown'
            devices.append({**d,'status':status})
        state.update(devices=devices,locations=self.cell['locations'],session=self.current,service_error=self._error,
                     mode='simulation',workflow=self.flow if not self.engine else json.loads(self.engine.store.db.execute('SELECT body FROM runs').fetchone()[0]))
        return state

    def _recipes(self):
        return [{'recipe':default_recipe(self.cell,self.flow),'builtin':True}]+self.panel.meta('recipes',[])

    def _save_recipe(self,payload):
        actor=payload.get('actor');expected=payload.get('expected_version');recipe=copy.deepcopy(payload.get('recipe'))
        if not isinstance(actor,str) or not actor.strip() or len(actor)>100:raise ValueError('Operator name is required')
        if type(expected) is not int or expected<0:raise ValueError('Template expected_version must be a non-negative integer')
        if not isinstance(recipe,dict) or not isinstance(recipe.get('id'),str):raise ValueError('流程标识必填')
        if recipe['id'].startswith('builtin_'):raise ValueError('内置流程请另存为新模板')
        records=self.panel.meta('recipes',[])
        version=max((r['recipe']['version'] for r in records if r['recipe']['id']==recipe['id']),default=0)
        if expected!=version:raise ConflictError('模板已更新，请重新加载最新版本，或另存为新模板')
        recipe['version']=version+1
        preview_recipe(self.cell,recipe)
        record={'recipe':recipe,'builtin':False,'actor':actor,'saved_at':datetime.now(timezone.utc).isoformat()}
        with self.panel.transaction():
            self.panel.set_meta('recipes',records+[record])
        return record

    @staticmethod
    def _faults(scenario,workflow):
        if scenario=='normal':return {}
        steps=workflow['steps']
        if scenario=='lost_ack':
            candidates=[s for s in steps if s['capability']=='liquid.transfer'] or [s for s in steps if s['kind']=='process']
        else:
            candidates=[s for s in steps if s['kind']=='transport']
        target=(candidates or steps)[0]
        return {target['id']:scenario}

    def _create(self,payload):
        actor=payload.get('actor');key=payload.get('key');scenario=payload.get('scenario','normal')
        if not isinstance(actor,str) or not actor.strip() or len(actor)>100:raise ValueError('Operator name is required')
        if not isinstance(key,str) or not key.strip() or len(key)>200:raise ValueError('Submission key is required')
        if scenario not in SCENARIOS:raise ValueError('Unknown simulation scenario')
        if 'recipe' in payload and 'workflow' in payload:raise ValueError('recipe 与 workflow 只能提交一个')
        workflow=preview_recipe(self.cell,payload['recipe'])['workflow'] if 'recipe' in payload else payload.get('workflow',self.flow)
        validate(self.cell,workflow)
        request_hash=digest({'scenario':scenario,'workflow':workflow})
        catalog=self.panel.meta('catalog',[])
        prior=next((r for r in catalog if r['key']==key),None)
        if prior:
            if prior['request_hash']!=request_hash:raise ConflictError('Submission key has a different payload')
            if self.current and prior['session_id']==self.current['session_id']:return self._state()
            raise ConflictError('This key belongs to a previous batch; inspect history')
        if self.engine:
            old=self.engine.snapshot()
            if old['state'] not in ('succeeded','cancelled') or old['locks']:
                raise ConflictError('Finish or reconcile the current batch before starting another')
        session_id=uuid4().hex;path=self.directory/'batches'/session_id
        faults=self._faults(scenario,workflow)
        router=DeviceRouter(self.cell,path/'devices',self.registry,faults=faults)
        engine=None
        try:
            engine=Engine(self.cell,path/'runtime.sqlite',router)
            run_id=engine.submit(workflow,key)
            with engine.store.transaction():engine.store.event(engine.now,'operator_submitted',run_id,{'actor':actor,'scenario':scenario})
            record={'session_id':session_id,'run_id':run_id,'key':key,'request_hash':request_hash,'scenario':scenario,
                    'faults':faults,'workflow_name':workflow.get('name',workflow['definition_id']),
                    'workflow_version':workflow['version'],
                    'created_at':datetime.now(timezone.utc).isoformat()}
            with self.panel.transaction():
                self.panel.set_meta('catalog',catalog+[record]);self.panel.set_meta('current',record)
        except BaseException:
            if engine:engine.close()
            router.close();raise
        if self.engine:self.engine.close()
        if self.router:self.router.close()
        self.engine=engine;self.router=router;self.current=record;self._error=None
        self._save_summary()
        return self._state()

    def _dispatch(self,action,payload):
        if action=='bootstrap':return {'workcell':self.cell,'workflow':self.flow,'driver_keys':self.registry.keys(),'mode':'simulation',
                                       'operation_catalog':operation_catalog(self.cell),'default_recipe':default_recipe(self.cell,self.flow)}
        if action=='recipes':return self._recipes()
        if action=='save_recipe':return self._save_recipe(payload)
        if action=='preview_recipe':return preview_recipe(self.cell,payload.get('recipe'))
        if action=='state':return self._state()
        if action=='history':
            self._save_summary()
            return [self.panel.meta('summary:'+r['session_id'],r) for r in reversed(self.panel.meta('catalog',[]))]
        if action=='drivers':return self.router.describe() if self.router else [{'driver':k} for k in self.registry.keys()]
        if action=='create':return self._create(payload)
        target_run=payload.pop('run_id',None)
        if target_run is not None and (not self.current or target_run!=self.current['run_id']):
            raise BatchNotFoundError('Batch is no longer the active batch')
        if not self.engine:raise ConflictError('Create a batch first')
        if action=='tick':self.engine.tick(self.engine.now+1)
        elif action in ('pause','resume','cancel','acknowledge_alarm','resolve'):
            getattr(self.engine,action)(**payload)
        elif action=='recover':
            with self.engine.store.transaction():
                self.engine._guard(payload.get('actor'),payload.get('expected_version'))
                self.engine.store.event(self.engine.now,'operator_recheck',self.current['run_id'],{'actor':payload['actor']})
            self.engine.recover();self._error=None
        else:raise ValueError('Unknown service action')
        self._save_summary()
        return self._state()

    def close(self):
        if self._closed:return
        if self.thread.is_alive():
            self.call('close');self.thread.join(20)
            if self.thread.is_alive():raise RuntimeError('Controller did not shut down')
        self._closed=True
