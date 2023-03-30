"""A hand-written bridge policy."""

import logging
from typing import Callable, Dict, Set

import numpy as np

from predicators import utils
from predicators.bridge_policies import BaseBridgePolicy, BridgePolicyDone
from predicators.settings import CFG
from predicators.structs import NSRT, BridgePolicy, DummyOption, GroundAtom, \
    Predicate, State, _GroundNSRT, _Option


class OracleBridgePolicy(BaseBridgePolicy):
    """A hand-written bridge policy."""

    def __init__(self, predicates: Set[Predicate], nsrts: Set[NSRT]) -> None:
        super().__init__(predicates, nsrts)
        self._oracle_bridge_policy = _create_oracle_bridge_policy(
            CFG.env, self._nsrts, self._predicates, self._rng)

    @classmethod
    def get_name(cls) -> str:
        return "oracle"

    def get_policy(self, failed_option: _Option) -> Callable[[State], _Option]:

        def _option_policy(state: State) -> _Option:
            atoms = utils.abstract(state, self._predicates)
            option = self._oracle_bridge_policy(state, atoms, failed_option)
            logging.debug(f"Using option {option.name}{option.objects} "
                          "from bridge policy.")
            return option

        return _option_policy


def _create_oracle_bridge_policy(env_name: str, nsrts: Set[NSRT],
                                 predicates: Set[Predicate],
                                 rng: np.random.Generator) -> BridgePolicy:
    nsrt_name_to_nsrt = {n.name: n for n in nsrts}
    pred_name_to_pred = {p.name: p for p in predicates}

    if env_name == "painting":
        return _create_painting_oracle_bridge_policy(nsrt_name_to_nsrt,
                                                     pred_name_to_pred, rng)

    if env_name == "stick_button":
        return _create_stick_button_oracle_bridge_policy(
            nsrt_name_to_nsrt, pred_name_to_pred, rng)

    raise NotImplementedError(f"No oracle bridge policy for {env_name}")


def _create_painting_oracle_bridge_policy(
        nsrt_name_to_nsrt: Dict[str, NSRT], pred_name_to_pred: Dict[str,
                                                                    Predicate],
        rng: np.random.Generator) -> BridgePolicy:

    PlaceOnTable = nsrt_name_to_nsrt["PlaceOnTable"]
    OpenLid = nsrt_name_to_nsrt["OpenLid"]

    Holding = pred_name_to_pred["Holding"]

    def _bridge_policy(state: State, atoms: Set[GroundAtom],
                       failed_option: _Option) -> _Option:
        lid = next(o for o in state if o.type.name == "lid")

        # If the box lid is already open, the bridge policy is done.
        # Second case should only happen when the shelf placements fail.
        if state.get(lid, "is_open") > 0.5 or failed_option.name != "Place":
            raise BridgePolicyDone()

        robot = failed_option.objects[0]
        held_objs = {a.objects[0] for a in atoms if a.predicate == Holding}

        if not held_objs:
            next_nsrt = OpenLid.ground([lid, robot])
        else:
            held_obj = next(iter(held_objs))
            next_nsrt = PlaceOnTable.ground([held_obj, robot])

        goal: Set[GroundAtom] = set()  # goal assumed not used by sampler
        return next_nsrt.sample_option(state, goal, rng)

    return _bridge_policy


def _create_stick_button_oracle_bridge_policy(
        nsrt_name_to_nsrt: Dict[str, NSRT], pred_name_to_pred: Dict[str,
                                                                    Predicate],
        rng: np.random.Generator) -> BridgePolicy:

    # TODO add to the state the history of failed NSRTs, and try to press
    # each button individually before resorting to using the stick?
    # Or, allow putting the stick down after it's grasped.
    # Either way, we need to allow the bridge policy itself to fail I think...
    # But then we need to commit to the bridge policy using NSRTs? Otherwise
    # the last-failed-NSRT concept doesn't work...
    # How much do we care about bridge-specific sampling?

    PickStickFromNothing = nsrt_name_to_nsrt["PickStickFromNothing"]

    Grasped = pred_name_to_pred["Grasped"]

    def _bridge_policy(state: State, atoms: Set[GroundAtom],
                       failed_option: _Option) -> _Option:

        robot = next(o for o in state if o.type.name == "robot")
        stick = next(o for o in state if o.type.name == "stick")

        if Grasped.holds(state, [robot, stick]):
            raise BridgePolicyDone()

        next_nsrt = PickStickFromNothing.ground([robot, stick])

        logging.debug(f"Using NSRT {next_nsrt.name}{next_nsrt.objects} "
                      "from bridge policy.")

        goal: Set[GroundAtom] = set()  # goal assumed not used by sampler
        return next_nsrt.sample_option(state, goal, rng)

    return _bridge_policy
