"""The bridge's consumer on the simulation thread: drain the queue between physics steps.

Kit-independent: runtime.py supplies the Isaac Sim calls (capture, world rebuild), and
tests supply fakes, so the same code applies commands in both.
"""
import traceback

from errors import CommandError


class Controller:
    def __init__(self, world, commands, frames, capture, load_world, lockstep, playing=lambda: True, log=None, on_ready=None):
        self.world, self.commands, self.frames = world, commands, frames
        self.capture, self.load_world = capture, load_world
        self.lockstep, self.playing = lockstep, playing
        self.log = log or (lambda command_id, command: None)
        self.on_ready = on_ready or (lambda command_id: None)
        self.pending = None  # Step in progress: {"id", "command", "steps", "remaining", "plan"}
        self.ready = False

    @property
    def waiting(self):
        """Lockstep with no step in progress: time must not advance."""
        return self.lockstep and self.pending is None

    @property
    def step_due(self):
        return self.pending is not None and self.pending["remaining"] == 0

    def service(self):
        """Apply queued commands in order; a step stops the drain until it has finished."""
        if self.pending is not None:
            return
        for command_id, command in self.commands.drain():
            self.commands.start(command_id)
            kind = command["type"]
            try:
                if kind == "step":
                    if not self.lockstep:
                        raise CommandError("step requires starting the harbor with --lockstep", code="not_lockstep")
                    steps, plan = self.world.begin_step(command)
                    self.pending = {"id": command_id, "command": command, "steps": steps, "remaining": steps, "plan": plan}
                    # Leave later commands queued until this step has rendered.
                    return
                if kind == "render":
                    self.world.check(command.get("checks", {}))
                    plan = self.world.capture_plan(command)
                    result = self.world.observation(0, self.capture(plan), self.frames, self.playing())
                elif kind == "load_world":
                    result = self.load_world(command["world"])
                else:
                    result = self.world.apply(command)
            except CommandError as error:
                self.commands.complete(command_id, error, sim_time_ns=self.world.sim_time_ns)
                continue
            except Exception as error:
                # A failed command must not stop a running simulation.
                traceback.print_exc()
                self.commands.complete(command_id, f"Internal error: {error}", sim_time_ns=self.world.sim_time_ns)
                continue
            # Publish first: a client that sees "applied" must then read the applied state.
            self.publish()
            if kind == "probe" and not self.ready:
                # Ready only once the bridge has serviced a command against the loaded scene.
                self.ready = True
                self.commands.mark_ready()
                self.on_ready(command_id)
            self.commands.complete(command_id, result=result, sim_time_ns=self.world.sim_time_ns)
            self.log(command_id, command)

    def stepped(self):
        """Called after each physics step."""
        if self.pending is not None:
            self.pending["remaining"] -= 1

    def finish_step(self):
        pending, self.pending = self.pending, None
        # The boundary belongs to the new step index; rendering does not move bodies, so the
        # step's holds can end before capture.
        self.world.end_step(pending["steps"])
        captured = self.capture(pending["plan"]) if pending["plan"] else {}
        observation = self.world.observation(pending["steps"] * self.world.period_ns, captured, self.frames, self.playing())
        self.publish()
        self.commands.complete(pending["id"], result=observation, sim_time_ns=self.world.sim_time_ns)
        self.log(pending["id"], pending["command"])

    def abort_step(self, message, code):
        if self.pending is not None:
            self.world.end_step(0)
            self.commands.complete(self.pending["id"], CommandError(message, code=code), sim_time_ns=self.world.sim_time_ns)
            self.pending = None

    def publish(self):
        self.commands.publish(self.world.snapshot(self.playing()))
