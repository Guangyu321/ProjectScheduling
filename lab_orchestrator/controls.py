"""Durable operator controls; mutations share the engine's transaction boundary."""
import json
from .storage import encode


class ConflictError(ValueError):
    """The submitted state version or transition is no longer applicable."""


class Controls:
    def _guard(self,actor,expected_version=None):
        if not isinstance(actor,str) or not actor.strip() or len(actor)>100:
            raise ValueError('Operator name is required (maximum 100 characters)')
        if expected_version is not None:
            if type(expected_version) is not int or expected_version!=self._version():
                raise ConflictError('State changed; refresh before submitting this action')
        run=self.store.db.execute('SELECT * FROM runs').fetchone()
        if not run:
            raise ConflictError('No batch has been submitted')
        return run

    def _version(self):
        # Operator decisions and recovery changes invalidate forms; routine
        # command progress must not make pause/cancel race every scheduler tick.
        kinds=('run_submitted','operator_submitted','pause_requested',
               'resume_requested','cancel_requested','alarm_acknowledged',
               'recovery_resolved','recovery_device_verified','blocked',
               'command_reconciled','run_succeeded','run_cancelled',
               'operator_recheck','scheduling_blocked')
        placeholders=','.join('?' for _ in kinds)
        return self.store.db.execute(
            f'SELECT COALESCE(MAX(seq),0) FROM events WHERE kind IN ({placeholders})',
            kinds).fetchone()[0]

    def pause(self,*,actor,expected_version=None):
        """Stop new dispatch and let in-flight actions reach a verified state."""
        with self.store.transaction():
            run=self._guard(actor,expected_version)
            if run['state'] in ('succeeded','cancelled') or self.store.meta('control_intent','run')=='cancel':
                raise ConflictError('This batch cannot be paused')
            self.store.set_meta('control_intent','pause')
            self.store.event(self.now,'pause_requested',run['id'],{'actor':actor})
        self._refresh_run()
        return self.snapshot()

    def resume(self,*,actor,expected_version=None):
        """Resume only after all uncertain commands have been reconciled."""
        with self.store.transaction() as db:
            run=self._guard(actor,expected_version)
            if run['state'] not in ('paused','pausing') or self.store.meta('control_intent')=='cancel':
                raise ConflictError('Only a paused or pausing batch may resume')
            if db.execute("SELECT 1 FROM recovery_cases WHERE state='open'").fetchone():
                raise ConflictError('Resolve open recovery cases first')
            if db.execute("SELECT 1 FROM steps WHERE state='blocked'").fetchone():
                raise ConflictError('Uncertain actions still hold resources')
            self.store.set_meta('control_intent','run')
            self.store.set_meta('scheduling_blocked',False)
            self.store.event(self.now,'resume_requested',run['id'],{'actor':actor})
        self._refresh_run()
        return self.snapshot()

    def cancel(self,*,actor,reason,expected_version=None):
        """Cancel unstarted work; never pretend an uncertain physical action stopped."""
        if not isinstance(reason,str) or not reason.strip() or len(reason)>2000:
            raise ValueError('Cancellation reason is required')
        with self.store.transaction() as db:
            run=self._guard(actor,expected_version)
            if run['state'] in ('succeeded','cancelled'):
                raise ConflictError('The batch has already ended')
            self.store.set_meta('control_intent','cancel')
            db.execute("UPDATE steps SET state='cancelled' WHERE state='pending'")
            self.store.event(self.now,'cancel_requested',run['id'],{'actor':actor,'reason':reason})
        self._refresh_run()
        return self.snapshot()

    def acknowledge_alarm(self,command_id,*,actor,expected_version=None):
        """Acknowledge awareness without clearing the root cause or resource locks."""
        with self.store.transaction() as db:
            self._guard(actor,expected_version)
            alarm=db.execute('SELECT * FROM alarms WHERE command_id=?',(command_id,)).fetchone()
            if not alarm:
                raise ValueError('Alarm not found')
            if alarm['state']=='resolved':
                raise ConflictError('Alarm is already resolved')
            db.execute("UPDATE alarms SET state='acknowledged' WHERE command_id=?",(command_id,))
            self.store.event(self.now,'alarm_acknowledged',command_id,{'actor':actor})
        return self.snapshot()

    def resolve(self,command_id,decision,*,actor,reason,evidence,expected_version=None):
        """Record an operator observation and atomically reconcile a blocked command.

        The operator must verify the device has stopped and identify the plate
        and its observed location. This is an audited assertion, not telemetry.
        """
        if decision not in ('completed','not_started'):
            raise ValueError('Decision must be completed or not_started')
        if not isinstance(reason,str) or not reason.strip() or len(reason)>2000:
            raise ValueError('Recovery reason is required')
        if not isinstance(evidence,dict) or evidence.get('device_stopped') is not True:
            raise ValueError('Evidence must confirm the device has stopped')
        observation=evidence.get('observation')
        if not isinstance(observation,str) or not observation.strip() or len(observation)>4000:
            raise ValueError('An observation record is required')
        with self.store.transaction() as db:
            self._guard(actor,expected_version)
            case=db.execute('SELECT * FROM recovery_cases WHERE command_id=?',(command_id,)).fetchone()
            command=db.execute('SELECT * FROM commands WHERE id=?',(command_id,)).fetchone()
            if not case or case['state']!='open' or not command or command['state']!='blocked':
                raise ConflictError('This recovery case is no longer open')
            step=json.loads(db.execute('SELECT body FROM steps WHERE id=?',(command['step_id'],)).fetchone()[0])
            required_location=(step['target'] if decision=='completed' else step['source']) if step['kind']=='transport' else step['location']
            if evidence.get('plate_id')!=step['plate_id'] or evidence.get('location')!=required_location:
                raise ValueError('Plate identity or observed location does not match this decision')
            occupancy=db.execute('SELECT id FROM plates WHERE location=?',(required_location,)).fetchone()
            if occupancy and occupancy['id']!=step['plate_id']:
                raise ConflictError('The observed location is occupied by another plate')
            if decision=='completed':
                result=evidence.get('result',{})
                if not isinstance(result,dict):
                    raise ValueError('Result must be an object')
                result=dict(result,plate_id=step['plate_id'],step_id=step['id'],manual_observation=True)
                if step['kind']=='transport':
                    result['confirmed_location']=required_location
                if step['capability']=='identity.scan' and evidence.get('barcode')!=step['parameters']['expected_barcode']:
                    raise ValueError('Observed barcode is required and must match')
                if step['capability']=='identity.scan':
                    result['barcode']=evidence['barcode']
                if step['capability']=='fluorescence.measure':
                    import math
                    if (type(result.get('value')) not in (int,float) or not math.isfinite(result['value'])
                            or not isinstance(result.get('unit'),str) or not result['unit'].strip()
                            or result.get('method_id')!=step['parameters']['method_id']):
                        raise ValueError('Measurement value, unit and matching method_id are required')
                self._apply_completion(command,step,{'ended':self.now,'result':result})
            else:
                db.execute("UPDATE commands SET state='not_started',ended=?,result=? WHERE id=?",
                           (self.now,encode({'manual_observation':True,'outcome':'not_started'}),command_id))
                next_state='cancelled' if self.store.meta('control_intent')=='cancel' else 'pending'
                db.execute('UPDATE steps SET state=? WHERE id=?',(next_state,step['id']))
                db.execute('UPDATE plates SET location=?,certainty=? WHERE id=?',(required_location,'known',step['plate_id']))
                db.execute('DELETE FROM locks WHERE command_id=?',(command_id,))
                db.execute("UPDATE alarms SET state='resolved' WHERE command_id=?",(command_id,))
            db.execute("UPDATE recovery_cases SET state='resolved',decision=?,actor=?,reason=?,evidence=? WHERE command_id=?",
                       (decision,actor,reason,encode(evidence),command_id))
            if self.store.meta('control_intent')!='cancel':
                self.store.set_meta('control_intent','pause')
            self.store.set_meta('scheduling_blocked',False)
            self.store.event(self.now,'recovery_resolved',command_id,
                             {'actor':actor,'decision':decision,'reason':reason,'evidence':evidence})
        self._refresh_run()
        return self.snapshot()
