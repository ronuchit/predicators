"""A TAMP approach that uses hand-specified NSRTs.

The approach is aware of the initial predicates and options.
Predicates that are not in the initial predicates are excluded from
the ground truth NSRTs. If an NSRT's option is not included,
that NSRT will not be generated at all.
"""

from typing import List, Sequence, Set
import numpy as np
from predicators.src.approaches import TAMPApproach
from predicators.src.envs import create_env, BlocksEnv, PaintingEnv, PlayroomEnv
from predicators.src.structs import NSRT, Predicate, State, \
    ParameterizedOption, Variable, Type, LiftedAtom, Object, Array
from predicators.src.settings import CFG


class OracleApproach(TAMPApproach):
    """A TAMP approach that uses hand-specified NSRTs.
    """
    @property
    def is_learning_based(self) -> bool:
        return False

    def _get_current_nsrts(self) -> Set[NSRT]:
        return get_gt_nsrts(self._initial_predicates, self._initial_options)


def get_gt_nsrts(predicates: Set[Predicate],
                 options: Set[ParameterizedOption]) -> Set[NSRT]:
    """Create ground truth NSRTs for an env.
    """
    if CFG.env in ("cover", "cover_hierarchical_types"):
        nsrts = _get_cover_gt_nsrts(options_are_typed=False)
    elif CFG.env == "cover_typed_options":
        nsrts = _get_cover_gt_nsrts(options_are_typed=True)
    elif CFG.env == "cover_multistep_options":
        nsrts = _get_cover_gt_nsrts(options_are_typed=True,
                                    include_robot_in_holding=True,
                                    place_sampler_relative=True)
    elif CFG.env == "cluttered_table":
        nsrts = _get_cluttered_table_gt_nsrts()
    elif CFG.env == "blocks":
        nsrts = _get_blocks_gt_nsrts()
    elif CFG.env == "painting":
        nsrts = _get_painting_gt_nsrts()
    elif CFG.env == "playroom":
        nsrts = _get_playroom_gt_nsrts()
    elif CFG.env == "repeated_nextto":
        nsrts = _get_repeated_nextto_gt_nsrts()
    else:
        raise NotImplementedError("Ground truth NSRTs not implemented")
    # Filter out excluded predicates/options
    final_nsrts = set()
    for nsrt in nsrts:
        if nsrt.option not in options:
            continue
        nsrt = nsrt.filter_predicates(predicates)
        final_nsrts.add(nsrt)
    return final_nsrts


def _get_from_env_by_names(env_name: str, names: Sequence[str],
                           env_attr: str) -> List:
    """Helper for loading types, predicates, and options by name.
    """
    env = create_env(env_name)
    name_to_env_obj = {}
    for o in getattr(env, env_attr):
        name_to_env_obj[o.name] = o
    assert set(name_to_env_obj).issuperset(set(names))
    return [name_to_env_obj[name] for name in names]


def _get_types_by_names(env_name: str,
                        names: Sequence[str]) -> List[Type]:
    """Load types from an env given their names.
    """
    return _get_from_env_by_names(env_name, names, "types")


def _get_predicates_by_names(env_name: str,
                             names: Sequence[str]) -> List[Predicate]:
    """Load predicates from an env given their names.
    """
    return _get_from_env_by_names(env_name, names, "predicates")


def _get_options_by_names(env_name: str,
                          names: Sequence[str]) -> List[ParameterizedOption]:
    """Load parameterized options from an env given their names.
    """
    return _get_from_env_by_names(env_name, names, "options")


def _get_cover_gt_nsrts(options_are_typed: bool,
                        include_robot_in_holding: bool = False,
                        place_sampler_relative: bool = False) -> Set[NSRT]:
    """Create ground truth NSRTs for CoverEnv.
    """
    block_type, target_type, robot_type = _get_types_by_names(
        CFG.env, ["block", "target", "robot"])

    IsBlock, IsTarget, Covers, HandEmpty, Holding = \
        _get_predicates_by_names(CFG.env, ["IsBlock", "IsTarget", "Covers",
                                           "HandEmpty", "Holding"])

    if options_are_typed:
        Pick, Place = _get_options_by_names(CFG.env, ["Pick", "Place"])
    else:
        PickPlace, = _get_options_by_names(CFG.env, ["PickPlace"])

    nsrts = set()

    # Pick
    block = Variable("?block", block_type)
    robot = Variable("?robot", robot_type)
    parameters = [block, robot] if include_robot_in_holding else [block]
    if options_are_typed:
        option_vars = [block]
        option = Pick
    else:
        option_vars = []
        option = PickPlace
    preconditions = {LiftedAtom(IsBlock, [block]), LiftedAtom(HandEmpty, [])}
    if include_robot_in_holding:
        add_effects = {LiftedAtom(Holding, [block, robot])}
    else:
        add_effects = {LiftedAtom(Holding, [block])}
    delete_effects = {LiftedAtom(HandEmpty, [])}
    def pick_sampler(state: State, rng: np.random.Generator,
                     objs: Sequence[Object]) -> Array:
        assert len(objs) == 2 if include_robot_in_holding else len(objs) == 1
        b = objs[0]
        assert b.is_instance(block_type)
        if options_are_typed:
            lb = float(-state.get(b, "width")/2)  # relative positioning only
            ub = float(state.get(b, "width")/2)  # relative positioning only
        else:
            lb = float(state.get(b, "pose") - state.get(b, "width")/2)
            lb = max(lb, 0.0)
            ub = float(state.get(b, "pose") + state.get(b, "width")/2)
            ub = min(ub, 1.0)
        return np.array(rng.uniform(lb, ub, size=(1,)), dtype=np.float32)
    pick_nsrt = NSRT("Pick", parameters, preconditions,
                     add_effects, delete_effects, option,
                     option_vars, pick_sampler)
    nsrts.add(pick_nsrt)

    # Place
    target = Variable("?target", target_type)
    parameters = [block, target, robot] if include_robot_in_holding \
        else [block, target]
    if options_are_typed:
        option_vars = [target]
        option = Place
    else:
        option_vars = []
        option = PickPlace
    add_effects = {LiftedAtom(HandEmpty, []),
                   LiftedAtom(Covers, [block, target])}
    if include_robot_in_holding:
        preconditions = {LiftedAtom(IsBlock, [block]),
                         LiftedAtom(IsTarget, [target]),
                         LiftedAtom(Holding, [block, robot])}
        delete_effects = {LiftedAtom(Holding, [block, robot])}
    else:
        preconditions = {LiftedAtom(IsBlock, [block]),
                         LiftedAtom(IsTarget, [target]),
                         LiftedAtom(Holding, [block])}
        delete_effects = {LiftedAtom(Holding, [block])}
    def place_sampler(state: State, rng: np.random.Generator,
                      objs: Sequence[Object]) -> Array:
        assert len(objs) == 3 if include_robot_in_holding else len(objs) == 2
        t = objs[1]
        assert t.is_instance(target_type)
        if place_sampler_relative:
            lb = float(-state.get(t, "width")/2)  # relative positioning only
            ub = float(state.get(t, "width")/2)  # relative positioning only
        else:
            lb = float(state.get(t, "pose") - state.get(t, "width")/10)
            lb = max(lb, 0.0)
            ub = float(state.get(t, "pose") + state.get(t, "width")/10)
            ub = min(ub, 1.0)
        return np.array(rng.uniform(lb, ub, size=(1,)), dtype=np.float32)
    place_nsrt = NSRT("Place", parameters, preconditions,
                      add_effects, delete_effects, option,
                      option_vars, place_sampler)
    nsrts.add(place_nsrt)

    return nsrts


def _get_cluttered_table_gt_nsrts() -> Set[NSRT]:
    """Create ground truth NSRTs for ClutteredTableEnv.
    """
    can_type, = _get_types_by_names("cluttered_table", ["can"])

    HandEmpty, Holding, Untrashed = _get_predicates_by_names(
        "cluttered_table", ["HandEmpty", "Holding", "Untrashed"])

    Grasp, Dump = _get_options_by_names("cluttered_table", ["Grasp", "Dump"])

    nsrts = set()

    # Grasp
    can = Variable("?can", can_type)
    parameters = [can]
    option_vars = [can]
    option = Grasp
    preconditions = {LiftedAtom(HandEmpty, []), LiftedAtom(Untrashed, [can])}
    add_effects = {LiftedAtom(Holding, [can])}
    delete_effects = {LiftedAtom(HandEmpty, [])}
    def grasp_sampler(state: State, rng: np.random.Generator,
                      objs: Sequence[Object]) -> Array:
        assert len(objs) == 1
        can = objs[0]
        end_x = state.get(can, "pose_x")
        end_y = state.get(can, "pose_y")
        start_x, start_y = rng.uniform(0.0, 1.0, size=2)  # start from anywhere
        return np.array([start_x, start_y, end_x, end_y], dtype=np.float32)
    grasp_nsrt = NSRT("Grasp", parameters, preconditions,
                      add_effects, delete_effects, option,
                      option_vars, grasp_sampler)
    nsrts.add(grasp_nsrt)

    # Dump
    can = Variable("?can", can_type)
    parameters = [can]
    option_vars = []
    option = Dump
    preconditions = {LiftedAtom(Holding, [can]), LiftedAtom(Untrashed, [can])}
    add_effects = {LiftedAtom(HandEmpty, [])}
    delete_effects = {LiftedAtom(Holding, [can]), LiftedAtom(Untrashed, [can])}
    dump_nsrt = NSRT("Dump", parameters, preconditions, add_effects,
                     delete_effects, option, option_vars,
                     lambda s, r, o: np.array([], dtype=np.float32))
    nsrts.add(dump_nsrt)

    return nsrts


def _get_blocks_gt_nsrts() -> Set[NSRT]:
    """Create ground truth NSRTs for BlocksEnv.
    """
    block_type, robot_type = _get_types_by_names("blocks", ["block", "robot"])

    On, OnTable, GripperOpen, Holding, Clear = _get_predicates_by_names(
        "blocks", ["On", "OnTable", "GripperOpen", "Holding", "Clear"])

    Pick, Stack, PutOnTable = _get_options_by_names(
        "blocks", ["Pick", "Stack", "PutOnTable"])

    nsrts = set()

    # PickFromTable
    block = Variable("?block", block_type)
    robot = Variable("?robot", robot_type)
    parameters = [block, robot]
    option_vars = [robot, block]
    option = Pick
    preconditions = {LiftedAtom(OnTable, [block]),
                     LiftedAtom(Clear, [block]),
                     LiftedAtom(GripperOpen, [robot])}
    add_effects = {LiftedAtom(Holding, [block])}
    delete_effects = {LiftedAtom(OnTable, [block]),
                      LiftedAtom(Clear, [block]),
                      LiftedAtom(GripperOpen, [robot])}
    def pick_sampler(state: State, rng: np.random.Generator,
                     objs: Sequence[Object]) -> Array:
        del state, rng, objs  # unused
        return np.zeros(3, dtype=np.float32)
    pickfromtable_nsrt = NSRT(
        "PickFromTable", parameters, preconditions, add_effects,
        delete_effects, option, option_vars, pick_sampler)
    nsrts.add(pickfromtable_nsrt)

    # Unstack
    block = Variable("?block", block_type)
    otherblock = Variable("?otherblock", block_type)
    robot = Variable("?robot", robot_type)
    parameters = [block, otherblock, robot]
    option_vars = [robot, block]
    option = Pick
    preconditions = {LiftedAtom(On, [block, otherblock]),
                     LiftedAtom(Clear, [block]),
                     LiftedAtom(GripperOpen, [robot])}
    add_effects = {LiftedAtom(Holding, [block]),
                   LiftedAtom(Clear, [otherblock])}
    delete_effects = {LiftedAtom(On, [block, otherblock]),
                      LiftedAtom(Clear, [block]),
                      LiftedAtom(GripperOpen, [robot])}
    unstack_nsrt = NSRT(
        "Unstack", parameters, preconditions, add_effects,
        delete_effects, option, option_vars, pick_sampler)
    nsrts.add(unstack_nsrt)

    # Stack
    block = Variable("?block", block_type)
    otherblock = Variable("?otherblock", block_type)
    robot = Variable("?robot", robot_type)
    parameters = [block, otherblock, robot]
    option_vars = [robot, otherblock]
    option = Stack
    preconditions = {LiftedAtom(Holding, [block]),
                     LiftedAtom(Clear, [otherblock])}
    add_effects = {LiftedAtom(On, [block, otherblock]),
                   LiftedAtom(Clear, [block]),
                   LiftedAtom(GripperOpen, [robot])}
    delete_effects = {LiftedAtom(Holding, [block]),
                      LiftedAtom(Clear, [otherblock])}
    def stack_sampler(state: State, rng: np.random.Generator,
                      objs: Sequence[Object]) -> Array:
        del state, rng, objs  # unused
        return np.array([0, 0, BlocksEnv.block_size], dtype=np.float32)
    stack_nsrt = NSRT(
        "Stack", parameters, preconditions, add_effects,
        delete_effects, option, option_vars, stack_sampler)
    nsrts.add(stack_nsrt)

    # PutOnTable
    block = Variable("?block", block_type)
    robot = Variable("?robot", robot_type)
    parameters = [block, robot]
    option_vars = [robot]
    option = PutOnTable
    preconditions = {LiftedAtom(Holding, [block])}
    add_effects = {LiftedAtom(OnTable, [block]),
                   LiftedAtom(Clear, [block]),
                   LiftedAtom(GripperOpen, [robot])}
    delete_effects = {LiftedAtom(Holding, [block])}
    def putontable_sampler(state: State, rng: np.random.Generator,
                           objs: Sequence[Object]) -> Array:
        del state, objs  # unused
        x = rng.uniform()
        y = rng.uniform()
        return np.array([x, y], dtype=np.float32)
    putontable_nsrt = NSRT(
        "PutOnTable", parameters, preconditions, add_effects,
        delete_effects, option, option_vars, putontable_sampler)
    nsrts.add(putontable_nsrt)

    return nsrts


def _get_painting_gt_nsrts() -> Set[NSRT]:
    """Create ground truth NSRTs for PaintingEnv.
    """
    obj_type, box_type, lid_type, shelf_type, robot_type = \
        _get_types_by_names("painting", ["obj", "box", "lid", "shelf", "robot"])

    (InBox, InShelf, IsBoxColor, IsShelfColor, GripperOpen, OnTable,
     HoldingTop, HoldingSide, Holding, IsWet, IsDry, IsDirty, IsClean) = \
         _get_predicates_by_names(
             "painting", ["InBox", "InShelf", "IsBoxColor", "IsShelfColor",
                          "GripperOpen", "OnTable", "HoldingTop", "HoldingSide",
                          "Holding", "IsWet", "IsDry", "IsDirty", "IsClean"])

    Pick, Wash, Dry, Paint, Place, OpenLid = _get_options_by_names(
        "painting", ["Pick", "Wash", "Dry", "Paint", "Place", "OpenLid"])

    nsrts = set()

    # PickFromTop
    obj = Variable("?obj", obj_type)
    robot = Variable("?robot", robot_type)
    parameters = [obj, robot]
    option_vars = [robot, obj]
    option = Pick
    preconditions = {LiftedAtom(GripperOpen, [robot]),
                     LiftedAtom(OnTable, [obj])}
    add_effects = {LiftedAtom(Holding, [obj]),
                   LiftedAtom(HoldingTop, [robot])}
    delete_effects = {LiftedAtom(GripperOpen, [robot])}
    def pickfromtop_sampler(state: State, rng: np.random.Generator,
                            objs: Sequence[Object]) -> Array:
        del state, rng, objs  # unused
        return np.array([0.0, 0.0, 0.0, 1.0], dtype=np.float32)
    pickfromtop_nsrt = NSRT(
        "PickFromTop", parameters, preconditions, add_effects,
        delete_effects, option, option_vars, pickfromtop_sampler)
    nsrts.add(pickfromtop_nsrt)

    # PickFromSide
    obj = Variable("?obj", obj_type)
    robot = Variable("?robot", robot_type)
    parameters = [obj, robot]
    option_vars = [robot, obj]
    option = Pick
    preconditions = {LiftedAtom(GripperOpen, [robot]),
                     LiftedAtom(OnTable, [obj])}
    add_effects = {LiftedAtom(Holding, [obj]),
                   LiftedAtom(HoldingSide, [robot])}
    delete_effects = {LiftedAtom(GripperOpen, [robot])}
    def pickfromside_sampler(state: State, rng: np.random.Generator,
                             objs: Sequence[Object]) -> Array:
        del state, rng, objs  # unused
        return np.array([0.0, 0.0, 0.0, 0.0], dtype=np.float32)
    pickfromside_nsrt = NSRT(
        "PickFromSide", parameters, preconditions, add_effects,
        delete_effects, option, option_vars, pickfromside_sampler)
    nsrts.add(pickfromside_nsrt)

    # Wash
    obj = Variable("?obj", obj_type)
    robot = Variable("?robot", robot_type)
    parameters = [obj, robot]
    option_vars = [robot]
    option = Wash
    preconditions = {LiftedAtom(Holding, [obj]),
                     LiftedAtom(IsDry, [obj]),
                     LiftedAtom(IsDirty, [obj])}
    add_effects = {LiftedAtom(IsWet, [obj]),
                   LiftedAtom(IsClean, [obj])}
    delete_effects = {LiftedAtom(IsDry, [obj]),
                      LiftedAtom(IsDirty, [obj])}
    def wash_sampler(state: State, rng: np.random.Generator,
                     objs: Sequence[Object]) -> Array:
        del state, rng, objs  # unused
        return np.array([1.0], dtype=np.float32)
    wash_nsrt = NSRT(
        "Wash", parameters, preconditions, add_effects,
        delete_effects, option, option_vars, wash_sampler)
    nsrts.add(wash_nsrt)

    # Dry
    obj = Variable("?obj", obj_type)
    robot = Variable("?robot", robot_type)
    parameters = [obj, robot]
    option_vars = [robot]
    option = Dry
    preconditions = {LiftedAtom(Holding, [obj]),
                     LiftedAtom(IsWet, [obj])}
    add_effects = {LiftedAtom(IsDry, [obj])}
    delete_effects = {LiftedAtom(IsWet, [obj])}
    def dry_sampler(state: State, rng: np.random.Generator,
                    objs: Sequence[Object]) -> Array:
        del state, rng, objs  # unused
        return np.array([1.0], dtype=np.float32)
    dry_nsrt = NSRT(
        "Dry", parameters, preconditions, add_effects,
        delete_effects, option, option_vars, dry_sampler)
    nsrts.add(dry_nsrt)

    # PaintToBox
    obj = Variable("?obj", obj_type)
    box = Variable("?box", box_type)
    robot = Variable("?robot", robot_type)
    parameters = [obj, box, robot]
    option_vars = [robot]
    option = Paint
    preconditions = {LiftedAtom(Holding, [obj]),
                     LiftedAtom(IsDry, [obj]),
                     LiftedAtom(IsClean, [obj])}
    add_effects = {LiftedAtom(IsBoxColor, [obj, box])}
    delete_effects = set()
    def painttobox_sampler(state: State, rng: np.random.Generator,
                           objs: Sequence[Object]) -> Array:
        del rng  # unused
        box_color = state.get(objs[1], "color")
        return np.array([box_color], dtype=np.float32)
    painttobox_nsrt = NSRT(
        "PaintToBox", parameters, preconditions, add_effects,
        delete_effects, option, option_vars, painttobox_sampler)
    nsrts.add(painttobox_nsrt)

    # PaintToShelf
    obj = Variable("?obj", obj_type)
    shelf = Variable("?shelf", shelf_type)
    robot = Variable("?robot", robot_type)
    parameters = [obj, shelf, robot]
    option_vars = [robot]
    option = Paint
    preconditions = {LiftedAtom(Holding, [obj]),
                     LiftedAtom(IsDry, [obj]),
                     LiftedAtom(IsClean, [obj])}
    add_effects = {LiftedAtom(IsShelfColor, [obj, shelf])}
    delete_effects = set()
    def painttoshelf_sampler(state: State, rng: np.random.Generator,
                             objs: Sequence[Object]) -> Array:
        del rng  # unused
        shelf_color = state.get(objs[1], "color")
        return np.array([shelf_color], dtype=np.float32)
    painttoshelf_nsrt = NSRT(
        "PaintToShelf", parameters, preconditions, add_effects,
        delete_effects, option, option_vars, painttoshelf_sampler)
    nsrts.add(painttoshelf_nsrt)

    # PlaceInBox
    obj = Variable("?obj", obj_type)
    box = Variable("?box", box_type)
    robot = Variable("?robot", robot_type)
    parameters = [obj, box, robot]
    option_vars = [robot]
    option = Place
    preconditions = {LiftedAtom(Holding, [obj]),
                     LiftedAtom(HoldingTop, [robot])}
    add_effects = {LiftedAtom(InBox, [obj, box]),
                   LiftedAtom(GripperOpen, [robot])}
    delete_effects = {LiftedAtom(HoldingTop, [robot]),
                      LiftedAtom(Holding, [obj]),
                      LiftedAtom(OnTable, [obj])}
    def placeinbox_sampler(state: State, rng: np.random.Generator,
                           objs: Sequence[Object]) -> Array:
        x = state.get(objs[0], "pose_x")
        y = rng.uniform(PaintingEnv.box_lb, PaintingEnv.box_ub)
        z = state.get(objs[0], "pose_z")
        return np.array([x, y, z], dtype=np.float32)
    placeinbox_nsrt = NSRT(
        "PlaceInBox", parameters, preconditions, add_effects,
        delete_effects, option, option_vars, placeinbox_sampler)
    nsrts.add(placeinbox_nsrt)

    # PlaceInShelf
    obj = Variable("?obj", obj_type)
    shelf = Variable("?shelf", shelf_type)
    robot = Variable("?robot", robot_type)
    parameters = [obj, shelf, robot]
    option_vars = [robot]
    option = Place
    preconditions = {LiftedAtom(Holding, [obj]),
                     LiftedAtom(HoldingSide, [robot])}
    add_effects = {LiftedAtom(InShelf, [obj, shelf]),
                   LiftedAtom(GripperOpen, [robot])}
    delete_effects = {LiftedAtom(HoldingSide, [robot]),
                      LiftedAtom(Holding, [obj]),
                      LiftedAtom(OnTable, [obj])}
    def placeinshelf_sampler(state: State, rng: np.random.Generator,
                             objs: Sequence[Object]) -> Array:
        x = state.get(objs[0], "pose_x")
        y = rng.uniform(PaintingEnv.shelf_lb, PaintingEnv.shelf_ub)
        z = state.get(objs[0], "pose_z")
        return np.array([x, y, z], dtype=np.float32)
    placeinshelf_nsrt = NSRT(
        "PlaceInShelf", parameters, preconditions, add_effects,
        delete_effects, option, option_vars, placeinshelf_sampler)
    nsrts.add(placeinshelf_nsrt)

    # OpenLid
    lid = Variable("?lid", lid_type)
    robot = Variable("?robot", robot_type)
    parameters = [lid, robot]
    option_vars = [robot, lid]
    option = OpenLid
    preconditions = {LiftedAtom(GripperOpen, [robot])}
    add_effects = set()
    delete_effects = set()
    def openlid_sampler(state: State, rng: np.random.Generator,
                        objs: Sequence[Object]) -> Array:
        del state, rng, objs  # unused
        return np.array([], dtype=np.float32)
    openlid_nsrt = NSRT(
        "OpenLid", parameters, preconditions, add_effects,
        delete_effects, option, option_vars, openlid_sampler)
    nsrts.add(openlid_nsrt)

    return nsrts

def _get_playroom_gt_nsrts() -> Set[NSRT]:
    """Create ground truth NSRTs for Playroom Env.
    """
    block_type, robot_type, door_type, dial_type = \
        _get_types_by_names(CFG.env, ["block", "robot", "door", "dial"])

    On, OnTable, GripperOpen, Holding, Clear, NextToTable, NextToDoor, \
        NextToDial, DoorOpen, DoorClosed, LightOn, LightOff = \
            _get_predicates_by_names(
"playroom", ["On", "OnTable", "GripperOpen", "Holding", "Clear", "NextToTable",
"NextToDoor", "NextToDial", "DoorOpen", "DoorClosed", "LightOn", "LightOff"])

    Pick, Stack, PutOnTable, Move, OpenDoor, CloseDoor, TurnOnDial, \
        TurnOffDial = _get_options_by_names("playroom",
        ["Pick", "Stack", "PutOnTable", "Move", "OpenDoor", "CloseDoor",
        "TurnOnDial", "TurnOffDial"])

    nsrts = set()

    # PickFromTable
    block = Variable("?block", block_type)
    robot = Variable("?robot", robot_type)
    parameters = [robot, block]
    option_vars = [robot, block]
    option = Pick
    preconditions = {LiftedAtom(OnTable, [block]),
                     LiftedAtom(Clear, [block]),
                     LiftedAtom(GripperOpen, [robot]),
                     LiftedAtom(NextToTable, [robot])}
    add_effects = {LiftedAtom(Holding, [block])}
    delete_effects = {LiftedAtom(OnTable, [block]),
                      LiftedAtom(Clear, [block]),
                      LiftedAtom(GripperOpen, [robot])}
    def pickfromtable_sampler(state: State, rng: np.random.Generator,
                     objs: Sequence[Object]) -> Array:
        del rng  # unused
        assert len(objs) == 2
        _, block = objs
        assert block.is_instance(block_type)
        # find rotation of robot that faces the table
        x, y = state.get(block, "pose_x"), state.get(block, "pose_y")
        cls = PlayroomEnv
        table_x = (cls.table_x_lb+cls.table_x_ub)/2
        table_y = (cls.table_y_lb+cls.table_y_ub)/2
        rotation = np.arctan2(table_y-y, table_x-x) / np.pi
        return np.array([0, 0, 0, rotation], dtype=np.float32)
    pickfromtable_nsrt = NSRT(
        "PickFromTable", parameters, preconditions, add_effects,
        delete_effects, option, option_vars, pickfromtable_sampler)
    nsrts.add(pickfromtable_nsrt)

    # Unstack
    block = Variable("?block", block_type)
    otherblock = Variable("?otherblock", block_type)
    robot = Variable("?robot", robot_type)
    parameters = [block, otherblock, robot]
    option_vars = [robot, block]
    option = Pick
    preconditions = {LiftedAtom(On, [block, otherblock]),
                     LiftedAtom(Clear, [block]),
                     LiftedAtom(GripperOpen, [robot]),
                     LiftedAtom(NextToTable, [robot])}
    add_effects = {LiftedAtom(Holding, [block]),
                   LiftedAtom(Clear, [otherblock])}
    delete_effects = {LiftedAtom(On, [block, otherblock]),
                      LiftedAtom(Clear, [block]),
                      LiftedAtom(GripperOpen, [robot])}
    def unstack_sampler(state: State, rng: np.random.Generator,
                     objs: Sequence[Object]) -> Array:
        del rng  # unused
        assert len(objs) == 3
        block, _, _ = objs
        assert block.is_instance(block_type)
        # find rotation of robot that faces the table
        x, y = state.get(block, "pose_x"), state.get(block, "pose_y")
        cls = PlayroomEnv
        table_x = (cls.table_x_lb+cls.table_x_ub)/2
        table_y = (cls.table_y_lb+cls.table_y_ub)/2
        rotation = np.arctan2(table_y-y, table_x-x) / np.pi
        return np.array([0, 0, 0, rotation], dtype=np.float32)
    unstack_nsrt = NSRT(
        "Unstack", parameters, preconditions, add_effects,
        delete_effects, option, option_vars, unstack_sampler)
    nsrts.add(unstack_nsrt)

    # Stack
    block = Variable("?block", block_type)
    otherblock = Variable("?otherblock", block_type)
    robot = Variable("?robot", robot_type)
    parameters = [block, otherblock, robot]
    option_vars = [robot, otherblock]
    option = Stack
    preconditions = {LiftedAtom(Holding, [block]),
                     LiftedAtom(Clear, [otherblock]),
                     LiftedAtom(NextToTable, [robot])}
    add_effects = {LiftedAtom(On, [block, otherblock]),
                   LiftedAtom(Clear, [block]),
                   LiftedAtom(GripperOpen, [robot])}
    delete_effects = {LiftedAtom(Holding, [block]),
                      LiftedAtom(Clear, [otherblock])}
    def stack_sampler(state: State, rng: np.random.Generator,
                      objs: Sequence[Object]) -> Array:
        del rng  # unused
        assert len(objs) == 3
        _, otherblock, _ = objs
        assert otherblock.is_instance(block_type)
        # find rotation of robot that faces the table
        x, y = state.get(otherblock, "pose_x"), state.get(otherblock, "pose_y")
        cls = PlayroomEnv
        table_x = (cls.table_x_lb+cls.table_x_ub)/2
        table_y = (cls.table_y_lb+cls.table_y_ub)/2
        rotation = np.arctan2(table_y-y, table_x-x) / np.pi
        return np.array([0, 0, PlayroomEnv.block_size, rotation],
                        dtype=np.float32)
    stack_nsrt = NSRT(
        "Stack", parameters, preconditions, add_effects,
        delete_effects, option, option_vars, stack_sampler)
    nsrts.add(stack_nsrt)

    # PutOnTable
    block = Variable("?block", block_type)
    robot = Variable("?robot", robot_type)
    parameters = [block, robot]
    option_vars = [robot]
    option = PutOnTable
    preconditions = {LiftedAtom(Holding, [block]),
                     LiftedAtom(NextToTable, [robot])}
    add_effects = {LiftedAtom(OnTable, [block]),
                   LiftedAtom(Clear, [block]),
                   LiftedAtom(GripperOpen, [robot])}
    delete_effects = {LiftedAtom(Holding, [block])}
    def putontable_sampler(state: State, rng: np.random.Generator,
                           objs: Sequence[Object]) -> Array:
        del state, objs  # unused
        x = rng.uniform()
        y = rng.uniform()
        # find rotation of robot that faces the table
        cls = PlayroomEnv
        table_x = (cls.table_x_lb+cls.table_x_ub)/2
        table_y = (cls.table_y_lb+cls.table_y_ub)/2
        rotation = np.arctan2(table_y-y, table_x-x) / np.pi
        return np.array([x, y, rotation], dtype=np.float32)
    putontable_nsrt = NSRT(
        "PutOnTable", parameters, preconditions, add_effects,
        delete_effects, option, option_vars, putontable_sampler)
    nsrts.add(putontable_nsrt)

    # MoveDoorToDoor
    robot = Variable("?robot", robot_type)
    fromdoor = Variable("?fromdoor", door_type)
    todoor = Variable("?todoor", door_type)
    parameters = [robot, fromdoor, todoor]
    option_vars = [robot]
    option = Move
    preconditions = {LiftedAtom(NextToDoor, [robot, fromdoor])}
    add_effects = {LiftedAtom(NextToDoor, [robot, todoor])}
    delete_effects = {LiftedAtom(NextToDoor, [robot, fromdoor])}
    def movedoortodoor_sampler(state: State, rng: np.random.Generator,
                           objs: Sequence[Object]) -> Array:
        del rng  # unused
        assert len(objs) == 3
        _, fromdoor, todoor = objs
        assert todoor.is_instance(door_type)
        to_x, to_y = state.get(todoor, "pose_x"), state.get(todoor, "pose_y")
        from_x = state.get(fromdoor, "pose_x")
        rotation = 0.0 if from_x < to_x else -1.0
        to_x = to_x-0.1 if from_x < to_x else to_x+0.1
        return np.array([to_x, to_y, rotation], dtype=np.float32)
    movedoortodoor_nsrt = NSRT(
        "MoveDoorToDoor", parameters, preconditions, add_effects,
        delete_effects, option, option_vars, movedoortodoor_sampler)
    nsrts.add(movedoortodoor_nsrt)

    # MoveTableToDoor
    robot = Variable("?robot", robot_type)
    door = Variable("?door", door_type)
    parameters = [robot, door]
    option_vars = [robot]
    option = Move
    preconditions = {LiftedAtom(NextToTable, [robot])}
    add_effects = {LiftedAtom(NextToDoor, [robot, door])}
    delete_effects = {LiftedAtom(NextToTable, [robot])}
    def movetabletodoor_sampler(state: State, rng: np.random.Generator,
                           objs: Sequence[Object]) -> Array:
        del rng  # unused
        assert len(objs) == 2
        _, door = objs
        assert door.is_instance(door_type)
        x, y = state.get(door, "pose_x")-0.1, state.get(door, "pose_y")
        return np.array([x, y, 0.0], dtype=np.float32)
    movetabletodoor_nsrt = NSRT(
        "MoveTableToDoor", parameters, preconditions, add_effects,
        delete_effects, option, option_vars, movetabletodoor_sampler)
    nsrts.add(movetabletodoor_nsrt)

    # MoveDoorToTable
    robot = Variable("?robot", robot_type)
    door = Variable("?door", door_type)
    parameters = [robot, door]
    option_vars = [robot]
    option = Move
    preconditions = {LiftedAtom(NextToDoor, [robot, door])}
    add_effects = {LiftedAtom(NextToTable, [robot])}
    delete_effects = {LiftedAtom(NextToDoor, [robot, door])}
    def movetotable_sampler(state: State, rng: np.random.Generator,
                            objs: Sequence[Object]) -> Array:
        del state, objs  # unused
        cls = PlayroomEnv
        x = rng.uniform(cls.table_x_lb, cls.table_x_ub)
        y = rng.uniform(cls.table_y_lb, cls.table_y_ub)
        # find rotation of robot that faces the table
        table_x = (cls.table_x_lb+cls.table_x_ub)/2
        table_y = (cls.table_y_lb+cls.table_y_ub)/2
        rotation = np.arctan2(table_y-y, table_x-x) / np.pi
        return np.array([x, y, rotation], dtype=np.float32)
    movedoortotable_nsrt = NSRT(
        "MoveDoorToTable", parameters, preconditions, add_effects,
        delete_effects, option, option_vars, movetotable_sampler)
    nsrts.add(movedoortotable_nsrt)

    # MoveDoorToDial
    robot = Variable("?robot", robot_type)
    door = Variable("?door", door_type)
    dial = Variable("?dial", dial_type)
    parameters = [robot, door, dial]
    option_vars = [robot]
    option = Move
    preconditions = {LiftedAtom(NextToDoor, [robot, door])}
    add_effects = {LiftedAtom(NextToDial, [robot, dial])}
    delete_effects = {LiftedAtom(NextToDoor, [robot, door])}
    def movetodial_sampler(state: State, rng: np.random.Generator,
                           objs: Sequence[Object]) -> Array:
        assert len(objs) == 3
        _, _, dial = objs
        assert dial.is_instance(dial_type)
        cls = PlayroomEnv
        dial_x, dial_y = state.get(dial, "pose_x"), state.get(dial, "pose_y")
        x = rng.uniform(dial_x-cls.dial_button_tol, dial_x+cls.dial_button_tol)
        y = rng.uniform(dial_y-cls.dial_button_tol, dial_y+cls.dial_button_tol)
        # find rotation of robot that faces the dial
        rotation = np.arctan2(dial_y-y, dial_x-x) / np.pi
        return np.array([x, y, rotation], dtype=np.float32)
    movedoortodial_nsrt = NSRT(
        "MoveDoorToDial", parameters, preconditions, add_effects,
        delete_effects, option, option_vars, movetodial_sampler)
    nsrts.add(movedoortodial_nsrt)

    # MoveDialToDoor
    robot = Variable("?robot", robot_type)
    dial = Variable("?dial", dial_type)
    door = Variable("?door", door_type)
    parameters = [robot, dial, door]
    option_vars = [robot]
    option = Move
    preconditions = {LiftedAtom(NextToDial, [robot, dial])}
    add_effects = {LiftedAtom(NextToDoor, [robot, door])}
    delete_effects = {LiftedAtom(NextToDial, [robot, dial])}
    def movedialtodoor_sampler(state: State, rng: np.random.Generator,
                           objs: Sequence[Object]) -> Array:
        del rng  # unused
        assert len(objs) == 3
        _, _, door = objs
        assert door.is_instance(door_type)
        x, y = state.get(door, "pose_x"), state.get(door, "pose_y")
        return np.array([x+0.1, y, -1.0], dtype=np.float32)
    movedialtodoor_nsrt = NSRT(
        "MoveDialToDoor", parameters, preconditions, add_effects,
        delete_effects, option, option_vars, movedialtodoor_sampler)
    nsrts.add(movedialtodoor_nsrt)

    # OpenDoor
    robot = Variable("?robot", robot_type)
    door = Variable("?door", door_type)
    parameters = [robot, door]
    option_vars = [door]
    option = OpenDoor
    preconditions = {LiftedAtom(NextToDoor, [robot, door]),
                     LiftedAtom(DoorClosed, [door]),
                     LiftedAtom(GripperOpen, [robot])}
    add_effects = {LiftedAtom(DoorOpen, [door])}
    delete_effects = {LiftedAtom(DoorClosed, [door])}
    def toggledoor_sampler(state: State, rng: np.random.Generator,
                           objs: Sequence[Object]) -> Array:
        del rng  # unused
        assert len(objs) == 2
        robot, door = objs
        assert robot.is_instance(robot_type)
        assert door.is_instance(door_type)
        x, y = state.get(robot, "pose_x"), state.get(robot, "pose_y")
        # find rotation of robot that faces the door
        door_x, door_y = state.get(door, "pose_x"), state.get(door, "pose_y")
        rotation = np.arctan2(door_y-y, door_x-x) / np.pi
        return np.array([0, 0, 0, rotation], dtype=np.float32)
    opendoor_nsrt = NSRT(
        "OpenDoor", parameters, preconditions, add_effects,
        delete_effects, option, option_vars, toggledoor_sampler)
    nsrts.add(opendoor_nsrt)

    # CloseDoor
    robot = Variable("?robot", robot_type)
    door = Variable("?door", door_type)
    parameters = [robot, door]
    option_vars = [door]
    option = CloseDoor
    preconditions = {LiftedAtom(NextToDoor, [robot, door]),
                     LiftedAtom(DoorOpen, [door]),
                     LiftedAtom(GripperOpen, [robot])}
    add_effects = {LiftedAtom(DoorClosed, [door])}
    delete_effects = {LiftedAtom(DoorOpen, [door])}
    closedoor_nsrt = NSRT(
        "CloseDoor", parameters, preconditions, add_effects,
        delete_effects, option, option_vars, toggledoor_sampler)
    nsrts.add(closedoor_nsrt)

    # TurnOnDial
    robot = Variable("?robot", robot_type)
    dial = Variable("?dial", dial_type)
    parameters = [robot, dial]
    option_vars = [dial]
    option = TurnOnDial
    preconditions = {LiftedAtom(NextToDial, [robot, dial]),
                     LiftedAtom(LightOff, [dial]),
                     LiftedAtom(GripperOpen, [robot])}
    add_effects = {LiftedAtom(LightOn, [dial])}
    delete_effects = {LiftedAtom(LightOff, [dial])}
    def toggledial_sampler(state: State, rng: np.random.Generator,
                         objs: Sequence[Object]) -> Array:
        del rng  # unused
        assert len(objs) == 2
        robot, dial = objs
        assert robot.is_instance(robot_type)
        assert door.is_instance(door_type)
        x, y = state.get(robot, "pose_x"), state.get(robot, "pose_y")
        # find rotation of robot that faces the dial
        dial_x, dial_y = state.get(dial, "pose_x"), state.get(dial, "pose_y")
        rotation = np.arctan2(dial_y-y, dial_x-x) / np.pi
        return np.array([0, 0, 0, rotation], dtype=np.float32)
    turnondial_nsrt = NSRT(
        "TurnOnDial", parameters, preconditions, add_effects,
        delete_effects, option, option_vars, toggledial_sampler)
    nsrts.add(turnondial_nsrt)

    # TurnOffDial
    robot = Variable("?robot", robot_type)
    dial = Variable("?dial", dial_type)
    parameters = [robot, dial]
    option_vars = [dial]
    option = TurnOffDial
    preconditions = {LiftedAtom(NextToDial, [robot, dial]),
                     LiftedAtom(LightOn, [dial]),
                     LiftedAtom(GripperOpen, [robot])}
    add_effects = {LiftedAtom(LightOff, [dial])}
    delete_effects = {LiftedAtom(LightOn, [dial])}
    turnoffdial_nsrt = NSRT(
        "TurnOffDial", parameters, preconditions, add_effects,
        delete_effects, option, option_vars, toggledial_sampler)
    nsrts.add(turnoffdial_nsrt)

    return nsrts

def _get_repeated_nextto_gt_nsrts() -> Set[NSRT]:
    """Create ground truth NSRTs for RepeatedNextToEnv.
    """
    robot_type, dot_type = _get_types_by_names(CFG.env, ["robot", "dot"])

    NextTo, NextToNothing, Grasped = _get_predicates_by_names(
        CFG.env, ["NextTo", "NextToNothing", "Grasped"])

    Move, Grasp = _get_options_by_names(CFG.env, ["Move", "Grasp"])

    nsrts = set()

    # Move from next to nothing
    robot = Variable("?robot", robot_type)
    targetdot = Variable("?targetdot", dot_type)
    parameters = [robot, targetdot]
    option_vars = [robot, targetdot]
    option = Move
    preconditions = {LiftedAtom(NextToNothing, [robot])}
    add_effects = {LiftedAtom(NextTo, [robot, targetdot])}
    delete_effects = {LiftedAtom(NextToNothing, [robot])}
    move_nsrt1 = NSRT("MoveFromNextToNothing", parameters, preconditions,
                      add_effects, delete_effects, option, option_vars,
                      lambda s, rng, o: np.zeros(1, dtype=np.float32))
    nsrts.add(move_nsrt1)

    # Move from next to something
    robot = Variable("?robot", robot_type)
    targetdot = Variable("?targetdot", dot_type)
    curdot = Variable("?curdot", dot_type)
    parameters = [robot, targetdot, curdot]
    option_vars = [robot, targetdot]
    option = Move
    preconditions = {LiftedAtom(NextTo, [robot, curdot])}
    add_effects = {LiftedAtom(NextTo, [robot, targetdot])}
    delete_effects = {LiftedAtom(NextTo, [robot, curdot])}
    move_nsrt2 = NSRT("MoveFromNextToSomething", parameters, preconditions,
                      add_effects, delete_effects, option, option_vars,
                      lambda s, rng, o: np.zeros(1, dtype=np.float32))
    nsrts.add(move_nsrt2)

    # Grasp and end up next to nothing
    robot = Variable("?robot", robot_type)
    targetdot = Variable("?targetdot", dot_type)
    parameters = [robot, targetdot]
    option_vars = [robot, targetdot]
    option = Grasp
    preconditions = {LiftedAtom(NextTo, [robot, targetdot])}
    add_effects = {LiftedAtom(Grasped, [robot, targetdot]),
                   LiftedAtom(NextToNothing, [robot])}
    delete_effects = {LiftedAtom(NextTo, [robot, targetdot])}
    grasp_nsrt1 = NSRT("GraspEndUpNextToNothing", parameters, preconditions,
                       add_effects, delete_effects, option, option_vars,
                       lambda s, rng, o: np.zeros(0, dtype=np.float32))
    nsrts.add(grasp_nsrt1)

    return nsrts
