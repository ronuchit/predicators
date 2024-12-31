"""Ground-truth NSRTs for the coffee environment."""

from typing import Dict, Set

from predicators.ground_truth_models import GroundTruthNSRTFactory
from predicators.structs import NSRT, DummyParameterizedOption, LiftedAtom, \
    ParameterizedOption, Predicate, Type, Variable
from predicators.utils import null_sampler


class PyBulletCircuitGroundTruthNSRTFactory(GroundTruthNSRTFactory):
    """Ground-truth NSRTs for the circuit environment."""

    @classmethod
    def get_env_names(cls) -> Set[str]:
        return {"pybullet_circuit"}

    @staticmethod
    def get_nsrts(env_name: str, types: Dict[str, Type],
                  predicates: Dict[str, Predicate],
                  options: Dict[str, ParameterizedOption]) -> Set[NSRT]:
        # Types
        robot_type = types["robot"]
        wire_type = types["wire"]
        light_type = types["light"]
        battery_type = types["battery"]

        # Predicates
        HandEmpty = predicates["HandEmpty"]
        Holding = predicates["Holding"]
        ConnectedToLight = predicates["ConnectedToLight"]
        ConnectedToBattery = predicates["ConnectedToBattery"]
        LightOn = predicates["LightOn"]
        CircuitClosed = predicates["CircuitClosed"]

        # Options
        PickConnector = options["PickConnector"]
        Connect = options["Connect"]

        nsrts = set()

        # PickConnector
        robot = Variable("?robot", robot_type)
        connector = Variable("?connector", wire_type)
        parameters = [robot, connector]
        option_vars = [robot, connector]
        option = PickConnector
        preconditions = {
            LiftedAtom(HandEmpty, [robot]),
        }
        add_effects = {
            LiftedAtom(Holding, [robot, connector]),
        }
        delete_effects = {
            LiftedAtom(HandEmpty, [robot]),
        }
        pick_connector_nsrt = NSRT("PickConnector", parameters,
                                   preconditions, add_effects, delete_effects,
                                   set(), option, option_vars, null_sampler)
        nsrts.add(pick_connector_nsrt)

        # ConnectFirstWire. Connect first wire to light and battery.
        wire = Variable("?wire", wire_type)
        light = Variable("?light", light_type)
        battery = Variable("?battery", battery_type)
        parameters = [wire, light, battery]
        option_vars = [wire, light, battery]
        option = Connect
        preconditions = {
            LiftedAtom(Holding, [robot, wire]),
            # Should add one that says the distance between the terminals are
            # close enough
        }
        add_effects = {
            LiftedAtom(ConnectedToLight, [wire, light]),
            LiftedAtom(ConnectedToBattery, [wire, battery]),
        }
        delete_effects = {
            LiftedAtom(Holding, [robot, wire]),
        }
        connect_first_wire_nsrt = NSRT("ConnectFirstWire", parameters,
                                            preconditions, add_effects,
                                            delete_effects, set(), option,
                                            option_vars, null_sampler)
        nsrts.add(connect_first_wire_nsrt)

        # hacky: connect second wire to light and power
        wire = Variable("?wire", wire_type)
        light = Variable("?light", light_type)
        battery = Variable("?battery", battery_type)
        parameters = [wire, light, battery]
        option_vars = [wire, light, battery]
        option = Connect
        preconditions = {
            LiftedAtom(Holding, [robot, wire]),
        }
        add_effects = {
            LiftedAtom(CircuitClosed, []),
            LiftedAtom(LightOn, [light]),
        }
        delete_effects = {
            LiftedAtom(Holding, [robot, wire]),
        }
        connect_second_wire_nsrt = NSRT("ConnectSecondWire", parameters,
                                            preconditions, add_effects,
                                            delete_effects, set(), option,
                                            option_vars, null_sampler)
        nsrts.add(connect_second_wire_nsrt)

        # # Done.
        # parameters = [Variable("?light", light_type)]
        # light = Variable("?light", light_type)
        # preconditions = {LiftedAtom(CircuitClosed, [])}
        # add_effects = {LiftedAtom(LightOn, [light])}
        # done_nsrt = NSRT("Done", [light], preconditions, add_effects, set(),
        #                  set(), DummyParameterizedOption, [], null_sampler)
        # nsrts.add(done_nsrt)

        return nsrts
