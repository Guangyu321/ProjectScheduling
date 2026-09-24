"""Laboratory orchestration simulation kernel. No real-device support."""
from .engine import Engine
from .simulator import Simulator
from .validation import validate


def run_demo(output_dir,faults=None):
    """Run the bundled two-plate simulation and save its reports."""
    from .demo import run_demo as execute
    return execute(output_dir,faults=faults)


__all__ = ['Engine','Simulator','validate','run_demo']
