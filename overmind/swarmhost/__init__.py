# overmind.swarmhost: dispatch pktrade sims to remote Hetzner boxes.
#
# Public API:
#   from overmind.swarmhost import Experiment, HostPool
#   exp = Experiment.create(name="armory", binary=Path("jammy.bin/pktrade"), confs=[...])
#   exp.push(pool)
#   result = exp.run(pool, jobs=[(conf_name, "20260101"), ...])
#
# See overmind/swarmhost/run.py for the CLI wrapper.

from .experiment import Experiment, HostPool, RunResult  # noqa: F401
