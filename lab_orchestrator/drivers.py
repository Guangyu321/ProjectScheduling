"""Explicit device plugins and durable per-command device routing."""
from pathlib import Path
from typing import Protocol,runtime_checkable
import copy
import hashlib
import sqlite3
from .simulator import Simulator
from .storage import digest


@runtime_checkable
class DeviceDriver(Protocol):
    """Implement this boundary for one device; constructors own connection setup.

    submit acknowledges acceptance, query returns physical evidence. Implementations
    must not retry unknown physical actions or advance workflow steps themselves.
    """
    def describe(self) -> dict: ...
    def health(self) -> dict: ...
    def submit(self,command_id: str,step: dict,now: float) -> dict: ...
    def query(self,command_id: str,now: float) -> dict: ...
    def close(self) -> None: ...


class DriverRegistry:
    def __init__(self):
        self._factories={}

    def register(self,key,factory):
        """Register an explicit factory(config, state_path); never import user strings."""
        if not isinstance(key,str) or not key.strip() or key in self._factories or not callable(factory):
            raise ValueError('Driver key must be unique and factory must be callable')
        self._factories[key]=factory

    def create(self,key,config,path):
        if key not in self._factories:
            raise ValueError(f'Driver is not registered: {key}')
        driver=self._factories[key](copy.deepcopy(config),Path(path))
        if not isinstance(driver,DeviceDriver):
            if hasattr(driver,'close'):driver.close()
            raise ValueError(f'Driver {key} does not implement DeviceDriver')
        return driver

    def keys(self):
        return sorted(self._factories)


def default_registry():
    registry=DriverRegistry()
    registry.register('simulated',lambda config,path:Simulator(path,faults=config.get('faults')))
    return registry


class DeviceRouter:
    """Persist routing before sending so a restart queries the original device."""
    def __init__(self,workcell,state_dir,registry,faults=None):
        directory=Path(state_dir).resolve();directory.mkdir(parents=True,exist_ok=True)
        self.path=directory/'routes.sqlite'
        self.drivers={};self.config=copy.deepcopy(workcell);self.db=None
        try:
            self.db=sqlite3.connect(self.path)
            self.db.row_factory=sqlite3.Row
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('CREATE TABLE IF NOT EXISTS routes(id TEXT PRIMARY KEY,device_id TEXT NOT NULL,payload_hash TEXT NOT NULL)')
            self.db.commit()
            for config in self.config['devices']:
                key=config['id']
                if key in self.drivers:raise ValueError('Duplicate device id')
                config=dict(config,faults=faults or {})
                filename=hashlib.sha256(key.encode('utf-8')).hexdigest()[:24]+'.sqlite'
                self.drivers[key]=registry.create(config['driver'],config,directory/filename)
        except BaseException:
            self.close();raise

    def describe(self):
        return [{'device_id':d['id'],'driver':d['driver'],'capabilities':d['capabilities'],
                 'features':self.drivers[d['id']].describe()} for d in self.config['devices']]

    def health(self):
        return {key:driver.health() for key,driver in self.drivers.items()}

    def submit(self,command_id,step,now):
        device_id=step['device_id']
        if device_id not in self.drivers:raise ValueError('Unregistered device')
        payload_hash=digest(step)
        with self.db:
            prior=self.db.execute('SELECT * FROM routes WHERE id=?',(command_id,)).fetchone()
            if prior and (prior['device_id']!=device_id or prior['payload_hash']!=payload_hash):
                raise ValueError('Command routing identity conflict')
            if not prior:
                self.db.execute('INSERT INTO routes VALUES(?,?,?)',(command_id,device_id,payload_hash))
        return self.drivers[device_id].submit(command_id,step,now)

    def query(self,command_id,now):
        row=self.db.execute('SELECT device_id FROM routes WHERE id=?',(command_id,)).fetchone()
        if not row:return {'state':'unknown'}
        return self.drivers[row['device_id']].query(command_id,now)

    def execution_count(self):
        return sum(driver.execution_count() for driver in self.drivers.values())

    def close(self):
        for driver in self.drivers.values():driver.close()
        self.drivers={}
        if self.db is not None:self.db.close();self.db=None
