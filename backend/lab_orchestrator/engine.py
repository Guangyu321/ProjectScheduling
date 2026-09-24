"""Single-writer, virtual-time execution kernel for the simulation milestone."""
import copy
import json
import math
from pathlib import Path
from uuid import uuid4
from .storage import Store,digest,encode
from .controls import Controls
from .validation import validate,resources_for


class Engine(Controls):
    """One database contains one batch; multiple plates within it may run concurrently.

    The caller owns the simulator and must close it separately. This synchronous
    virtual-time API is deliberately not a real-device driver execution loop.
    """
    def __init__(self,workcell,db_path,simulator):
        if Path(db_path).resolve()==getattr(simulator,'path',None):
            raise ValueError('Device ledger and controller database must be separate')
        self.cell=copy.deepcopy(workcell)
        self.simulator=simulator
        self.store=Store(db_path)
        try:
            prior=self.store.meta('workcell_hash')
            if prior is not None and prior!=digest(self.cell):
                raise ValueError('Stored workcell differs from supplied configuration')
            with self.store.transaction():
                self.store.set_meta('workcell_hash',digest(self.cell))
                self.store.set_meta('workcell',self.cell)
            self.now=self.store.meta('now',0.0)
        except BaseException:
            self.close(); raise

    def submit(self,workflow,key):
        """Validate and durably create a batch; a repeated key returns its identity."""
        validate(self.cell,workflow)
        if not isinstance(key,str) or not key.strip():
            raise ValueError('A non-empty idempotency key is required')
        body_hash=digest(workflow)
        with self.store.transaction() as db:
            prior=db.execute('SELECT * FROM runs WHERE request_key=?',(key,)).fetchone()
            if prior:
                if prior['payload_hash']!=body_hash:
                    raise ValueError('Idempotency key payload conflict')
                return prior['id']
            if db.execute('SELECT 1 FROM runs').fetchone():
                raise ValueError('v0.1 supports one batch per database; use a new database for another batch')
            run_id=str(uuid4())
            db.execute('INSERT INTO runs VALUES(?,?,?,?,?)',(run_id,key,body_hash,encode(workflow),'running'))
            for step in workflow['steps']:
                db.execute('INSERT INTO steps VALUES(?,?,?,?)',(step['id'],run_id,encode(step),'pending'))
            for plate in workflow['plates']:
                db.execute('INSERT INTO plates VALUES(?,?,?)',(plate['id'],plate['initial_location'],'known'))
            self.store.event(self.now,'run_submitted',run_id,{'definition_id':workflow['definition_id'],'version':workflow['version']})
        return run_id

    def _block(self,command,step,reason):
        with self.store.transaction() as db:
            prior=db.execute('SELECT state FROM commands WHERE id=?',(command['id'],)).fetchone()[0]
            db.execute('UPDATE commands SET state=? WHERE id=?',('blocked',command['id']))
            db.execute('UPDATE steps SET state=? WHERE id=?',('blocked',step['id']))
            db.execute('UPDATE runs SET state=?',('blocked',))
            if self.store.meta('control_intent')!='cancel':
                self.store.set_meta('control_intent','pause')
            db.execute("INSERT INTO recovery_cases VALUES(?, 'open', NULL, NULL, ?, '{}') ON CONFLICT(command_id) DO UPDATE SET state='open',reason=excluded.reason",(command['id'],reason))
            if step['kind']=='transport':
                db.execute('UPDATE plates SET certainty=? WHERE id=?',('unknown',step['plate_id']))
            db.execute("INSERT INTO alarms VALUES(?,?,?) ON CONFLICT(command_id) DO UPDATE SET reason=excluded.reason,state=CASE WHEN alarms.state='acknowledged' THEN 'acknowledged' ELSE excluded.state END",
                       (command['id'],reason,'open'))
            if prior!='blocked':
                self.store.event(self.now,'blocked',step['id'],{'command_id':command['id'],'reason':reason})

    def _complete(self,command,step,evidence):
        result=evidence.get('result',{})
        if not isinstance(result,dict):
            self._block(command,step,'Invalid completion evidence')
            return
        ended=evidence.get('ended')
        valid=(result.get('plate_id')==step['plate_id'] and result.get('step_id')==step['id']
               and type(ended) in (int,float) and math.isfinite(ended)
               and command['started']<=ended<=self.now)
        if step['kind']=='transport':
            valid=valid and result.get('confirmed_location')==step['target']
        if step['capability']=='identity.scan':
            valid=valid and result.get('barcode')==step['parameters']['expected_barcode']
        if not valid:
            self._block(command,step,'Invalid completion evidence')
            return
        with self.store.transaction():
            self._apply_completion(command,step,evidence)

    def _apply_completion(self,command,step,evidence):
        # Caller holds the transaction, including operator evidence when manual.
        db=self.store.db
        ended=evidence['ended']; result=evidence['result']
        if step['kind']=='transport':
            plate=db.execute('SELECT location FROM plates WHERE id=?',(step['plate_id'],)).fetchone()
            occupied=db.execute('SELECT id FROM plates WHERE location=?',(step['target'],)).fetchone()
            if plate['location']!=step['source'] or occupied:
                raise RuntimeError('Persisted plate positions contradict reserved transport')
            db.execute('UPDATE plates SET location=?,certainty=? WHERE id=?',(step['target'],'known',step['plate_id']))
        db.execute('UPDATE commands SET state=?,ended=?,result=? WHERE id=?',('succeeded',ended,encode(result),command['id']))
        db.execute('UPDATE steps SET state=? WHERE id=?',('succeeded',step['id']))
        db.execute('DELETE FROM locks WHERE command_id=?',(command['id'],))
        db.execute('UPDATE alarms SET state=? WHERE command_id=?',('resolved',command['id']))
        self.store.event(self.now,'step_succeeded',step['id'],{'command_id':command['id'],'result':result,'actual_end':ended})

        case=db.execute("SELECT state FROM recovery_cases WHERE command_id=?",(command['id'],)).fetchone()
        if case and case['state']=='open' and not result.get('manual_observation'):
            self.store.event(self.now,'recovery_device_verified',command['id'])
        db.execute("UPDATE recovery_cases SET state='resolved',decision=COALESCE(decision,'device_verified') WHERE command_id=?",(command['id'],))

    def _poll(self):
        db=self.store.db
        commands=db.execute("SELECT * FROM commands WHERE state NOT IN ('succeeded','not_started','cancelled') ORDER BY started,id").fetchall()
        for command in commands:
            step=json.loads(db.execute('SELECT body FROM steps WHERE id=?',(command['step_id'],)).fetchone()[0])
            try:
                evidence=self.simulator.query(command['id'],self.now)
            except Exception as exc:
                self._block(command,step,f'Query failed: {type(exc).__name__}: {exc}')
                continue
            if not isinstance(evidence,dict):
                self._block(command,step,'Malformed device evidence')
                continue
            state=evidence.get('state')
            if state=='succeeded':
                self._complete(command,step,evidence)
            elif state=='running' and self.now-command['started']<step['timeout_s']:
                with self.store.transaction():
                    db.execute('UPDATE commands SET state=? WHERE id=?',('running',command['id']))
                    db.execute('UPDATE steps SET state=? WHERE id=?',('running',step['id']))
                    db.execute('UPDATE alarms SET state=? WHERE command_id=?',('resolved',command['id']))
                    db.execute("UPDATE recovery_cases SET state='resolved',decision='device_running' WHERE command_id=?",(command['id'],))
                    if command['state']!='running':
                        self.store.event(self.now,'command_reconciled',step['id'],{'command_id':command['id'],'state':'running'})
            else:
                self._block(command,step,'Execution deadline exceeded' if state=='running' else 'Physical outcome unknown')
        self._refresh_run()

    def _refresh_run(self):
        db=self.store.db
        run=db.execute('SELECT * FROM runs').fetchone()
        if not run:
            return
        if run['state'] in ('succeeded','cancelled'):
            return
        states=[r[0] for r in db.execute('SELECT state FROM steps')]
        intent=self.store.meta('control_intent','run')
        active=any(s in ('dispatching','running','reconciling') for s in states)
        if 'blocked' in states:
            state='blocked'
        elif intent=='cancel':
            state='cancelling' if active else 'cancelled'
        elif all(s=='succeeded' for s in states):
            state='succeeded'
        elif intent=='pause':
            state='pausing' if active else 'paused'
        elif self.store.meta('scheduling_blocked',False):
            state='blocked'
        else:
            state='running'
        if state!=run['state']:
            with self.store.transaction():
                db.execute('UPDATE runs SET state=?',(state,))
                self.store.event(self.now,'run_'+state,run['id'])

    def _reserve(self,step):
        required=resources_for(self.cell,step)
        devices={d['id']:d for d in self.cell['devices']}
        locations={p['id']:p for p in self.cell['locations']}
        positions=[step['source'],step['target']] if step['kind']=='transport' else [step['location']]
        involved={step['device_id']} | {locations[p]['device_id'] for p in positions if locations[p]['device_id']}
        with self.store.transaction() as db:
            if any(devices[owner]['state']!='idle' for owner in involved):
                return None
            for resource in required:
                if db.execute('SELECT 1 FROM locks WHERE resource=?',(resource,)).fetchone():
                    return None
            if step['kind']=='transport':
                gripper=step['device_id']+'.slot1'
                held=db.execute('SELECT id FROM plates WHERE location=?',(gripper,)).fetchone()
                if held and (held['id']!=step['plate_id'] or step['source']!=gripper):
                    return None
            plate=db.execute('SELECT * FROM plates WHERE id=?',(step['plate_id'],)).fetchone()
            source=step['source'] if step['kind']=='transport' else step['location']
            if plate['certainty']!='known' or plate['location']!=source:
                return None
            if step['kind']=='transport' and db.execute('SELECT 1 FROM plates WHERE location=?',(step['target'],)).fetchone():
                return None
            command_id=str(uuid4())
            attempt=db.execute('SELECT COALESCE(MAX(attempt),0)+1 FROM commands WHERE step_id=?',(step['id'],)).fetchone()[0]
            db.execute('INSERT INTO commands VALUES(?,?,?,?,?,?,?,?)',(command_id,step['id'],'dispatching',self.now,None,encode(required),None,attempt))
            db.executemany('INSERT INTO locks VALUES(?,?)',[(r,command_id) for r in required])
            db.execute('UPDATE steps SET state=? WHERE id=?',('dispatching',step['id']))
            if step['kind']=='transport':
                db.execute('UPDATE plates SET certainty=? WHERE id=?',('unknown',step['plate_id']))
            self.store.event(self.now,'command_intent',step['id'],{'command_id':command_id,'resources':required})
        return command_id

    def tick(self,now):
        """Advance virtual time, reconcile in-flight work, then dispatch ready steps."""
        if type(now) not in (int,float) or not math.isfinite(now) or now<self.now:
            raise ValueError('Virtual time must be finite and monotonic')
        self.now=float(now)
        with self.store.transaction():
            self.store.set_meta('now',self.now)
        self._poll()
        db=self.store.db
        run=db.execute('SELECT state FROM runs').fetchone()
        if not run or run['state']!='running':
            return
        rows=db.execute('SELECT * FROM steps ORDER BY id').fetchall()
        states={r['id']:r['state'] for r in rows}
        for row in rows:
            if row['state']!='pending':
                continue
            step=json.loads(row['body'])
            if any(states[dep]!='succeeded' for dep in step['depends_on']):
                continue
            command_id=self._reserve(step)
            if command_id is None:
                continue
            try:
                self.simulator.submit(command_id,step,self.now)
            except Exception as exc:
                with self.store.transaction():
                    db.execute('UPDATE commands SET state=? WHERE id=?',('reconciling',command_id))
                    db.execute('UPDATE steps SET state=? WHERE id=?',('reconciling',step['id']))
                    self.store.event(self.now,'reconciling',step['id'],{'command_id':command_id,'reason':str(exc)})
                self._poll()
                if db.execute('SELECT state FROM runs').fetchone()[0]=='blocked':
                    break
            else:
                with self.store.transaction():
                    db.execute('UPDATE commands SET state=? WHERE id=?',('running',command_id))
                    db.execute('UPDATE steps SET state=? WHERE id=?',('running',step['id']))
                    self.store.event(self.now,'command_accepted',step['id'],{'command_id':command_id})

    def recover(self):
        """Query existing command identities; never redispatch uncertain actions."""
        with self.store.transaction():
            self.store.event(self.now,'recovery_started','station')
        self._poll()

    def run_until_complete(self,max_ticks=10000):
        """Run bounded one-second simulation ticks; return on success or blocking."""
        if type(max_ticks) is not int or max_ticks<=0:
            raise ValueError('max_ticks must be a positive integer')
        if not self.store.db.execute('SELECT 1 FROM runs').fetchone():
            raise ValueError('Submit a workflow first')
        for _ in range(max_ticks):
            self.tick(self.now)
            result=self.snapshot()
            if result['state'] in ('succeeded','blocked','paused','cancelled'):
                return result
            active=self.store.db.execute("SELECT 1 FROM commands WHERE state IN ('dispatching','running','reconciling')").fetchone()
            if not active:
                with self.store.transaction():
                    self.store.set_meta('scheduling_blocked',True)
                    self.store.db.execute('UPDATE runs SET state=?',('blocked',))
                    self.store.event(self.now,'scheduling_blocked',result['run_id'],{'reason':'No runnable step: resource, occupancy or device state conflict'})
                return self.snapshot()
            next_time=self.now+1
            with self.store.transaction():
                self.store.set_meta('now',next_time)
            self.now=next_time
        raise TimeoutError('Simulation tick budget exhausted; durable state retained')

    def snapshot(self):
        """Return serializable state, actual intervals, alarms and audit events."""
        db=self.store.db
        run=db.execute('SELECT * FROM runs').fetchone()
        plates=db.execute('SELECT * FROM plates ORDER BY id').fetchall()
        commands=[]
        for row in db.execute('SELECT * FROM commands ORDER BY started,step_id'):
            item=dict(row); item['resources']=json.loads(item['resources'])
            item['result']=json.loads(item['result']) if item['result'] else None
            commands.append(item)
        events=[]
        for row in db.execute('SELECT * FROM events ORDER BY seq'):
            item=dict(row); item['body']=json.loads(item['body']); events.append(item)
        return {'run_id':run['id'] if run else None,'state':run['state'] if run else 'empty','sim_time':self.now,'version':self._version(),
                'control_intent':self.store.meta('control_intent','run'),
                'recoveries':[{**dict(r),'evidence':json.loads(r['evidence'])} for r in db.execute('SELECT * FROM recovery_cases ORDER BY command_id')],
                'positions':{p['id']:p['location'] for p in plates},'certainty':{p['id']:p['certainty'] for p in plates},
                'steps':[dict(r) for r in db.execute('SELECT id,state FROM steps ORDER BY id')],
                'commands':commands,'locks':[dict(r) for r in db.execute('SELECT * FROM locks ORDER BY resource')],
                'alarms':[dict(r) for r in db.execute('SELECT * FROM alarms ORDER BY command_id')],'events':events}

    def close(self):
        """Release the controller connection/lock without stopping device actions."""
        self.store.close()
