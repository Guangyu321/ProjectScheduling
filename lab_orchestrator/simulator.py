"""Persistent virtual devices; never connect to hardware."""
from pathlib import Path
import json
import sqlite3
from .storage import digest,encode


class Simulator:
    def __init__(self,path,faults=None):
        self.path=Path(path).resolve()
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.faults=dict(faults or {})
        if any(f not in ('lost_ack','unknown') for f in self.faults.values()):
            raise ValueError('Unsupported simulated fault')
        self.db=sqlite3.connect(self.path)
        self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('''CREATE TABLE IF NOT EXISTS actions (
          id TEXT PRIMARY KEY,payload_hash TEXT NOT NULL,step_id TEXT NOT NULL,
          started REAL NOT NULL,ended REAL NOT NULL,fault TEXT NOT NULL,result TEXT NOT NULL)''')
        self.db.commit()

    def submit(self,command_id,step,now):
        """Durably record a single action before acknowledging it."""
        prior=self.db.execute('SELECT payload_hash FROM actions WHERE id=?',(command_id,)).fetchone()
        if prior:
            if prior[0]!=digest(step):
                raise ValueError('Command id payload conflict')
            return self.query(command_id,now)
        prior_attempt=self.db.execute('SELECT 1 FROM actions WHERE step_id=?',(step['id'],)).fetchone()
        fault='' if prior_attempt else self.faults.get(step['id'],'')
        result={'simulated':True,'step_id':step['id'],'plate_id':step['plate_id']}
        if step['kind']=='transport':
            result['confirmed_location']=step['target']
        elif step['capability']=='identity.scan':
            result['barcode']=step['parameters']['expected_barcode']
        elif step['capability']=='fluorescence.measure':
            result.update(value=100.0,unit='SIM_AU',method_id=step['parameters']['method_id'])
        with self.db:
            self.db.execute('INSERT INTO actions VALUES(?,?,?,?,?,?,?)',
              (command_id,digest(step),step['id'],now,now+step['estimated_duration_s'],fault,encode(result)))
        if fault in ('lost_ack','unknown'):
            raise TimeoutError('Simulated acknowledgement loss')
        return {'state':'running'}

    def query(self,command_id,now):
        row=self.db.execute('SELECT * FROM actions WHERE id=?',(command_id,)).fetchone()
        if row is None or row['fault']=='unknown':
            return {'state':'unknown'}
        if now<row['ended']:
            return {'state':'running'}
        return {'state':'succeeded','ended':row['ended'],'result':json.loads(row['result'])}

    def describe(self):
        return {'name':'Persistent simulation driver','simulation_only':True,
                'supports_query':True,'supports_dedup':True,'supports_pause':False,
                'supports_stop':False,'supports_position_feedback':True}

    def health(self):
        return {'online':self.db is not None,'mode':'simulation'}

    def execution_count(self):
        return self.db.execute('SELECT COUNT(*) FROM actions').fetchone()[0]

    def close(self):
        if self.db is not None:
            self.db.close(); self.db=None
