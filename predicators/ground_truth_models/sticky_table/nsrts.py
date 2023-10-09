"""Ground-truth NSRTs for the sticky table environment."""

from typing import Dict, Sequence, Set

import numpy as np

from predicators.envs.sticky_table import StickyTableEnv
from predicators.ground_truth_models import GroundTruthNSRTFactory
from predicators.structs import NSRT, Array, GroundAtom, LiftedAtom, Object, \
    ParameterizedOption, Predicate, State, Type, Variable


class StickyTableGroundTruthNSRTFactory(GroundTruthNSRTFactory):
    """Ground-truth NSRTs for the sticky table environment."""

    @classmethod
    def get_env_names(cls) -> Set[str]:
        return {"sticky_table", "sticky_table_tricky_floor"}

    @staticmethod
    def get_nsrts(env_name: str, types: Dict[str, Type],
                  predicates: Dict[str, Predicate],
                  options: Dict[str, ParameterizedOption]) -> Set[NSRT]:

        # Types
        robot_type = types["robot"]
        cube_type = types["cube"]
        ball_type = types["ball"]
        cup_type = types["cup"]
        table_type = types["table"]

        # Predicates
        CubeOnTable = predicates["CubeOnTable"]
        CubeOnFloor = predicates["CubeOnFloor"]
        HoldingCube = predicates["HoldingCube"]
        BallOnTable = predicates["BallOnTable"]
        BallOnFloor = predicates["BallOnFloor"]
        HoldingBall = predicates["HoldingBall"]
        CupOnTable = predicates["CupOnTable"]
        CupOnFloor = predicates["CupOnFloor"]
        HoldingCup = predicates["HoldingCup"]
        HandEmpty = predicates["HandEmpty"]
        ReachableCube = predicates["IsReachableCube"]
        ReachableSurface = predicates["IsReachableSurface"]
        ReachableBall = predicates["IsReachableBall"]
        ReachableCup = predicates["IsReachableCup"]
        BallInCup = predicates["BallInCup"]
        BallNotInCup = predicates["BallNotInCup"]

        # Options
        PickCubeFromTable = options["PickCubeFromTable"]
        PickCubeFromFloor = options["PickCubeFromFloor"]
        PlaceCubeOnTable = options["PlaceCubeOnTable"]
        PlaceCubeOnFloor = options["PlaceCubeOnFloor"]
        NavigateToCube = options["NavigateToCube"]
        NavigateToTable = options["NavigateToTable"]
        PickBallFromTable = options["PickBallFromTable"]
        PickBallFromFloor = options["PickBallFromFloor"]
        PlaceBallOnTable = options["PlaceBallOnTable"]
        PlaceBallOnFloor = options["PlaceBallOnFloor"]
        PickCupWithoutBallFromTable = options["PickCupWithoutBallFromTable"]
        PickCupWithBallFromTable = options["PickCupWithBallFromTable"]
        PickCupWithoutBallFromFloor = options["PickCupWithoutBallFromFloor"]
        PickCupWithBallFromFloor = options["PickCupWithBallFromFloor"]
        PlaceCupWithBallOnTable = options["PlaceCupWithBallOnTable"]
        PlaceCupWithoutBallOnTable = options["PlaceCupWithoutBallOnTable"]
        PlaceCupWithBallOnFloor = options["PlaceCupWithBallOnFloor"]
        PlaceCupWithoutBallOnFloor = options["PlaceCupWithoutBallOnFloor"]
        PlaceBallInCupOnFloor = options["PlaceBallInCupOnFloor"]
        PlaceBallInCupOnTable = options["PlaceBallInCupOnTable"]
        NavigateToBall = options["NavigateToBall"]
        NavigateToCube = options["NavigateToCube"]
        NavigateToCup = options["NavigateToCup"]

        nsrts = set()

        # PickCubeFromTable
        robot = Variable("?robot", robot_type)
        cube = Variable("?cube", cube_type)
        table = Variable("?table", table_type)
        parameters = [robot, cube, table]
        option_vars = parameters
        option = PickCubeFromTable
        preconditions = {
            LiftedAtom(ReachableSurface, [robot, table]),
            LiftedAtom(CubeOnTable, [cube, table]),
            LiftedAtom(HandEmpty, []),
        }
        add_effects = {LiftedAtom(HoldingCube, [cube])}
        delete_effects = {
            LiftedAtom(CubeOnTable, [cube, table]),
            LiftedAtom(HandEmpty, []),
        }

        def pick_obj_sampler(state: State, goal: Set[GroundAtom],
                             rng: np.random.Generator,
                             objs: Sequence[Object]) -> Array:
            # Sample within ball around center of the object.
            del goal  # unused
            obj = objs[1]
            if obj.type.name == "cube":
                obj_type_id = 0.0
                size = state.get(obj, "size") / 2
            else:
                if obj.type.name == "ball":
                    obj_type_id = 1.0
                else:
                    assert obj.type.name == "cup"
                    obj_type_id = 2.0
                size = state.get(obj, "radius")
            obj_x = state.get(obj, "x") + size / 2
            obj_y = state.get(obj, "y") + size / 2
            dist = rng.uniform(0, size / 4)
            theta = rng.uniform(0, 2 * np.pi)
            x = obj_x + dist * np.cos(theta)
            y = obj_y + dist * np.sin(theta)
            return np.array([1.0, obj_type_id, x, y], dtype=np.float32)

        pickcubefromtable_nsrt = NSRT("PickCubeFromTable", parameters,
                                      preconditions, add_effects,
                                      delete_effects, set(), option,
                                      option_vars, pick_obj_sampler)
        nsrts.add(pickcubefromtable_nsrt)

        # PickCubeFromFloor
        parameters = [robot, cube]
        option_vars = parameters
        option = PickCubeFromFloor
        preconditions = {
            LiftedAtom(ReachableCube, [robot, cube]),
            LiftedAtom(CubeOnFloor, [cube]),
            LiftedAtom(HandEmpty, []),
        }
        add_effects = {LiftedAtom(HoldingCube, [cube])}
        delete_effects = {
            LiftedAtom(CubeOnFloor, [cube]),
            LiftedAtom(HandEmpty, []),
        }

        pickcubefromfloor_nsrt = NSRT("PickCubeFromFloor", parameters,
                                      preconditions, add_effects,
                                      delete_effects, set(), option,
                                      option_vars, pick_obj_sampler)
        nsrts.add(pickcubefromfloor_nsrt)

        # PickBallFromTable
        robot = Variable("?robot", robot_type)
        ball = Variable("?ball", ball_type)
        table = Variable("?table", table_type)
        parameters = [robot, ball, table]
        option_vars = parameters
        option = PickBallFromTable
        preconditions = {
            LiftedAtom(ReachableSurface, [robot, table]),
            LiftedAtom(BallOnTable, [ball, table]),
            LiftedAtom(HandEmpty, []),
        }
        add_effects = {LiftedAtom(HoldingBall, [ball])}
        delete_effects = {
            LiftedAtom(BallOnTable, [ball, table]),
            LiftedAtom(HandEmpty, []),
        }
        ignore_effects = {BallInCup}
        pickballfromtable_nsrt = NSRT("PickBallFromTable", parameters,
                                      preconditions, add_effects,
                                      delete_effects, ignore_effects, option,
                                      option_vars, pick_obj_sampler)
        nsrts.add(pickballfromtable_nsrt)

        # PickBallFromFloor
        parameters = [robot, ball]
        option_vars = parameters
        option = PickBallFromFloor
        preconditions = {
            LiftedAtom(ReachableBall, [robot, ball]),
            LiftedAtom(BallOnFloor, [ball]),
            LiftedAtom(HandEmpty, []),
        }
        add_effects = {LiftedAtom(HoldingBall, [ball])}
        delete_effects = {
            LiftedAtom(BallOnFloor, [ball]),
            LiftedAtom(HandEmpty, []),
        }
        ignore_effects = {BallInCup}
        pickballfromfloor_nsrt = NSRT("PickBallFromFloor", parameters,
                                      preconditions, add_effects,
                                      delete_effects, ignore_effects, option,
                                      option_vars, pick_obj_sampler)
        nsrts.add(pickballfromfloor_nsrt)

        # PickCupWithoutBallFromTable
        robot = Variable("?robot", robot_type)
        cup = Variable("?cup", cup_type)
        ball = Variable("?ball", ball_type)
        table = Variable("?table", table_type)
        parameters = [robot, cup, ball, table]
        option_vars = parameters
        option = PickCupWithoutBallFromTable
        preconditions = {
            LiftedAtom(BallNotInCup, [ball, cup]),
            LiftedAtom(ReachableSurface, [robot, table]),
            LiftedAtom(CupOnTable, [cup, table]),
            LiftedAtom(HandEmpty, []),
        }
        add_effects = {LiftedAtom(HoldingCup, [cup])}
        delete_effects = {
            LiftedAtom(CupOnTable, [cup, table]),
            LiftedAtom(HandEmpty, []),
        }
        pickcupwithoutballfromtable_nsrt = NSRT("PickCupWithoutBallFromTable", parameters,
                                     preconditions, add_effects,
                                     delete_effects, set(), option,
                                     option_vars, pick_obj_sampler)
        nsrts.add(pickcupwithoutballfromtable_nsrt)

        # PickCupWithBallFromTable
        robot = Variable("?robot", robot_type)
        cup = Variable("?cup", cup_type)
        ball = Variable("?ball", ball_type)
        table = Variable("?table", table_type)
        parameters = [robot, cup, ball, table]
        option_vars = parameters
        option = PickCupWithBallFromTable
        preconditions = {
            LiftedAtom(BallInCup, [ball, cup]),
            LiftedAtom(ReachableSurface, [robot, table]),
            LiftedAtom(CupOnTable, [cup, table]),
            LiftedAtom(HandEmpty, []),
            LiftedAtom(BallOnTable, [ball, table])
        }
        add_effects = {LiftedAtom(HoldingCup, [cup]), LiftedAtom(HoldingBall, [ball])}
        delete_effects = {
            LiftedAtom(CupOnTable, [cup, table]),
            LiftedAtom(HandEmpty, []),
            LiftedAtom(BallOnTable, [ball, table])
        }
        pickcupwithoutballfromtable_nsrt = NSRT("PickCupWithBallFromTable", parameters,
                                     preconditions, add_effects,
                                     delete_effects, set(), option,
                                     option_vars, pick_obj_sampler)
        nsrts.add(pickcupwithoutballfromtable_nsrt)

        # PickCupWithoutBallFromFloor
        parameters = [robot, cup, ball]
        option_vars = parameters
        option = PickCupWithoutBallFromFloor
        preconditions = {
            LiftedAtom(BallNotInCup, [ball, cup]),
            LiftedAtom(ReachableCup, [robot, cup]),
            LiftedAtom(CupOnFloor, [cup]),
            LiftedAtom(HandEmpty, []),
        }
        add_effects = {LiftedAtom(HoldingCup, [cup])}
        delete_effects = {
            LiftedAtom(CupOnFloor, [cup]),
            LiftedAtom(HandEmpty, []),
        }
        pickcupwithoutballfromfloor_nsrt = NSRT("PickCupWithoutBallFromFloor", parameters,
                                     preconditions, add_effects,
                                     delete_effects, set(), option,
                                     option_vars, pick_obj_sampler)
        nsrts.add(pickcupwithoutballfromfloor_nsrt)

        # PickCupWithBallFromFloor
        parameters = [robot, cup, ball]
        option_vars = parameters
        option = PickCupWithBallFromFloor
        preconditions = {
            LiftedAtom(BallOnFloor, [ball]),
            LiftedAtom(BallInCup, [ball, cup]),
            LiftedAtom(ReachableCup, [robot, cup]),
            LiftedAtom(CupOnFloor, [cup]),
            LiftedAtom(HandEmpty, []),
        }
        add_effects = {LiftedAtom(HoldingCup, [cup]), LiftedAtom(HoldingBall, [ball])}
        delete_effects = {
            LiftedAtom(CupOnFloor, [cup]),
            LiftedAtom(HandEmpty, []),
            LiftedAtom(BallOnFloor, [ball])
        }
        pickcupwithballfromfloor_nsrt = NSRT("PickCupWithBallFromFloor", parameters,
                                     preconditions, add_effects,
                                     delete_effects, set(), option,
                                     option_vars, pick_obj_sampler)
        nsrts.add(pickcupwithballfromfloor_nsrt)

        # PlaceCubeOnTable
        parameters = [robot, cube, table]
        option_vars = parameters
        option = PlaceCubeOnTable
        preconditions = {
            LiftedAtom(ReachableSurface, [robot, table]),
            LiftedAtom(HoldingCube, [cube])
        }
        add_effects = {
            LiftedAtom(CubeOnTable, [cube, table]),
            LiftedAtom(HandEmpty, []),
        }
        delete_effects = {LiftedAtom(HoldingCube, [cube])}

        def place_on_table_sampler(state: State, goal: Set[GroundAtom],
                                   rng: np.random.Generator,
                                   objs: Sequence[Object]) -> Array:
            del goal  # unused
            table = objs[-1]
            obj = objs[-2]
            table_x = state.get(table, "x")
            table_y = state.get(table, "y")
            table_radius = state.get(table, "radius")
            if obj.type.name == "cube":
                size = state.get(obj, "size")
            else:
                assert obj.type.name in ["ball", "cup"]
                size = state.get(obj, "radius") * 2
            dist = rng.uniform(0, table_radius - size)
            theta = rng.uniform(0, 2 * np.pi)
            x = table_x + dist * np.cos(theta)
            y = table_y + dist * np.sin(theta)
            # NOTE: set obj_type_id to 3.0, since we want to
            # place onto the table!
            return np.array([1.0, 3.0, x, y], dtype=np.float32)

        placecubeontable_nsrt = NSRT("PlaceCubeOnTable", parameters,
                                     preconditions, add_effects,
                                     delete_effects, set(), option,
                                     option_vars, place_on_table_sampler)
        nsrts.add(placecubeontable_nsrt)

        # PlaceCubeOnFloor
        parameters = [robot, cube]
        option_vars = parameters
        option = PlaceCubeOnFloor
        preconditions = {LiftedAtom(HoldingCube, [cube])}
        add_effects = {
            LiftedAtom(CubeOnFloor, [cube]),
            LiftedAtom(HandEmpty, []),
        }
        delete_effects = {LiftedAtom(HoldingCube, [cube])}

        def place_on_floor_sampler(state: State, goal: Set[GroundAtom],
                                   rng: np.random.Generator,
                                   objs: Sequence[Object]) -> Array:
            del state, goal, rng, objs  # not used
            # Just place in the center of the room.
            x = (StickyTableEnv.x_lb + StickyTableEnv.x_ub) / 2
            y = (StickyTableEnv.y_lb + StickyTableEnv.y_ub) / 2
            # NOTE: obj_type_id set to 0.0 since it doesn't matter.
            return np.array([1.0, 0.0, x, y], dtype=np.float32)

        placecubeonfloor_nsrt = NSRT("PlaceCubeOnFloor", parameters,
                                     preconditions, add_effects,
                                     delete_effects, set(), option,
                                     option_vars, place_on_floor_sampler)
        nsrts.add(placecubeonfloor_nsrt)

        # PlaceBallOnTable
        parameters = [robot, ball, table]
        option_vars = parameters
        option = PlaceBallOnTable
        preconditions = {
            LiftedAtom(ReachableSurface, [robot, table]),
            LiftedAtom(HoldingBall, [ball])
        }
        add_effects = {
            LiftedAtom(BallOnTable, [ball, table]),
            LiftedAtom(HandEmpty, []),
        }
        delete_effects = {LiftedAtom(HoldingBall, [ball])}
        placeballontable_nsrt = NSRT("PlaceBallOnTable", parameters,
                                     preconditions, add_effects,
                                     delete_effects, set(), option,
                                     option_vars, place_on_table_sampler)
        nsrts.add(placeballontable_nsrt)

        # PlaceBallOnFloor
        parameters = [robot, ball]
        option_vars = parameters
        option = PlaceBallOnFloor
        preconditions = {LiftedAtom(HoldingBall, [ball])}
        add_effects = {
            LiftedAtom(BallOnFloor, [ball]),
            LiftedAtom(HandEmpty, []),
        }
        delete_effects = {LiftedAtom(HoldingBall, [ball])}
        placeballonfloor_nsrt = NSRT("PlaceBallOnFloor", parameters,
                                     preconditions, add_effects,
                                     delete_effects, set(), option,
                                     option_vars, place_on_floor_sampler)
        nsrts.add(placeballonfloor_nsrt)

        # PlaceBallInCupOnFloor
        parameters = [robot, ball, cup]
        option_vars = parameters
        option = PlaceBallInCupOnFloor
        preconditions = {
            LiftedAtom(BallNotInCup, [ball, cup]),
            LiftedAtom(ReachableCup, [robot, cup]),
            LiftedAtom(CupOnFloor, [cup]),
            LiftedAtom(HoldingBall, [ball])
        }
        add_effects = {
            LiftedAtom(BallInCup, [ball, cup]),
            LiftedAtom(BallOnFloor, [ball]),
            LiftedAtom(HandEmpty, []),
        }
        delete_effects = {LiftedAtom(HoldingBall, [ball]), LiftedAtom(BallNotInCup, [ball, cup])}

        def place_ball_in_cup_sampler(state: State, goal: Set[GroundAtom],
                                      rng: np.random.Generator,
                                      objs: Sequence[Object]) -> Array:
            del goal  # unused
            cup = objs[2]
            # Just place the ball in the middle of the cup. Set
            # the type id to be 2.0 to correspond to the cup
            return np.array(
                [1.0, 2.0, state.get(cup, "x"),
                 state.get(cup, "y")],
                dtype=np.float32)

        placeballincuponfloor_nsrt = NSRT("PlaceBallInCupOnFloor", parameters,
                                          preconditions, add_effects,
                                          delete_effects, set(), option,
                                          option_vars,
                                          place_ball_in_cup_sampler)
        nsrts.add(placeballincuponfloor_nsrt)

        # PlaceBallInCupOnTable
        parameters = [robot, ball, cup, table]
        option_vars = parameters
        option = PlaceBallInCupOnTable
        preconditions = {
            LiftedAtom(ReachableCup, [robot, cup]),
            LiftedAtom(CupOnTable, [cup, table]),
            LiftedAtom(HoldingBall, [ball]),
            LiftedAtom(BallNotInCup, [ball, cup]),
        }
        add_effects = {
            LiftedAtom(BallInCup, [ball, cup]),
            LiftedAtom(BallOnTable, [ball, table]),
            LiftedAtom(HandEmpty, []),
        }
        delete_effects = {LiftedAtom(HoldingBall, [ball]), LiftedAtom(BallNotInCup, [ball, cup]),}
        placeballincupontable_nsrt = NSRT("PlaceBallInCupOnTable", parameters,
                                          preconditions, add_effects,
                                          delete_effects, set(), option,
                                          option_vars,
                                          place_ball_in_cup_sampler)
        nsrts.add(placeballincupontable_nsrt)

        # PlaceCupWithoutBallOnTable
        parameters = [robot, ball, cup, table]
        option_vars = parameters
        option = PlaceCupWithoutBallOnTable
        preconditions = {
            LiftedAtom(ReachableSurface, [robot, table]),
            LiftedAtom(HoldingCup, [cup]),
            LiftedAtom(BallNotInCup, [ball, cup])
        }
        add_effects = {
            LiftedAtom(CupOnTable, [cup, table]),
            LiftedAtom(HandEmpty, []),
        }
        delete_effects = {LiftedAtom(HoldingCup, [cup])}
        placecupwithoutballontable_nsrt = NSRT("PlaceCupWithoutBallOnTable",
                                               parameters, preconditions,
                                               add_effects, delete_effects,
                                               set(), option, option_vars,
                                               place_on_table_sampler)
        nsrts.add(placecupwithoutballontable_nsrt)

        # PlaceCupWithBallOnTable
        parameters = [robot, ball, cup, table]
        option_vars = parameters
        option = PlaceCupWithBallOnTable
        preconditions = {
            LiftedAtom(ReachableSurface, [robot, table]),
            LiftedAtom(HoldingCup, [cup]),
            LiftedAtom(BallInCup, [ball, cup])
        }
        add_effects = {
            LiftedAtom(CupOnTable, [cup, table]),
            LiftedAtom(HandEmpty, []),
            LiftedAtom(BallOnTable, [ball, table])
        }
        delete_effects = {LiftedAtom(HoldingCup, [cup])}
        placecupwithballontable_nsrt = NSRT("PlaceCupWithBallOnTable",
                                            parameters, preconditions,
                                            add_effects, delete_effects, set(),
                                            option, option_vars,
                                            place_on_table_sampler)
        nsrts.add(placecupwithballontable_nsrt)

        # PlaceCupWithoutBallOnFloor
        parameters = [robot, ball, cup]
        option_vars = parameters
        option = PlaceCupWithoutBallOnFloor
        preconditions = {
            LiftedAtom(HoldingCup, [cup]),
            LiftedAtom(BallNotInCup, [ball, cup])
        }
        add_effects = {
            LiftedAtom(CupOnFloor, [cup]),
            LiftedAtom(HandEmpty, []),
        }
        delete_effects = {LiftedAtom(HoldingCup, [cup])}
        placecupwithoutballonfloor_nsrt = NSRT("PlaceCupWithoutBallOnFloor",
                                               parameters, preconditions,
                                               add_effects, delete_effects,
                                               set(), option, option_vars,
                                               place_on_floor_sampler)
        nsrts.add(placecupwithoutballonfloor_nsrt)

        # PlaceCupWithBallOnFloor
        parameters = [robot, ball, cup]
        option_vars = parameters
        option = PlaceCupWithBallOnFloor
        preconditions = {
            LiftedAtom(HoldingCup, [cup]),
            LiftedAtom(BallInCup, [ball, cup])
        }
        add_effects = {
            LiftedAtom(CupOnFloor, [cup]),
            LiftedAtom(HandEmpty, []),
            LiftedAtom(BallOnFloor, [ball])
        }
        delete_effects = {LiftedAtom(HoldingCup, [cup])}
        placecupwithballonfloor_nsrt = NSRT("PlaceCupWithBallOnFloor",
                                            parameters, preconditions,
                                            add_effects, delete_effects, set(),
                                            option, option_vars,
                                            place_on_floor_sampler)
        nsrts.add(placecupwithballonfloor_nsrt)

        # NavigateToCube
        parameters = [robot, cube]
        option_vars = parameters
        option = NavigateToCube
        preconditions = set()
        add_effects = {ReachableCube([robot, cube])}
        ignore_effects = {ReachableSurface, ReachableBall, ReachableCube, ReachableCup}

        def navigate_to_obj_sampler(state: State, goal: Set[GroundAtom],
                                    rng: np.random.Generator,
                                    objs: Sequence[Object]) -> Array:
            del goal  # not used
            robot, obj = objs
            if obj.type.name == "cube":
                size = state.get(obj, "size") / 2
            else:
                assert obj.type.name in ["table", "cup", "ball"]
                size = state.get(obj, "radius")
            obj_x = state.get(obj, "x")
            obj_y = state.get(obj, "y")
            max_dist = StickyTableEnv.reachable_thresh
            # NOTE: This must terminate for the problem to
            # be feasible, which it is basically guaranteed to
            # be, so there should be no worries that this will
            # loop forever.
            while True:
                dist = rng.uniform(size, max_dist)
                theta = rng.uniform(0, 2 * np.pi)
                x = obj_x + dist * np.cos(theta)
                y = obj_y + dist * np.sin(theta)
                pseudo_next_state = state.copy()
                pseudo_next_state.set(robot, "x", x)
                pseudo_next_state.set(robot, "y", y)
                if not StickyTableEnv.exists_robot_collision(
                        pseudo_next_state):
                    break
            # NOTE: obj_type_id set to 0.0 since it doesn't matter.
            return np.array([0.0, 0.0, x, y], dtype=np.float32)

        navigatetocube_nsrt = NSRT("NavigateToCube", parameters, preconditions,
                                   add_effects, set(), ignore_effects, option,
                                   option_vars, navigate_to_obj_sampler)
        nsrts.add(navigatetocube_nsrt)

        # NavigateToBall
        parameters = [robot, ball]
        option_vars = parameters
        option = NavigateToBall
        preconditions = set()
        add_effects = {ReachableBall([robot, ball])}
        ignore_effects = {ReachableSurface, ReachableBall, ReachableCube, ReachableCup}
        navigatetoball_nsrt = NSRT("NavigateToBall", parameters, preconditions,
                                   add_effects, set(), ignore_effects, option,
                                   option_vars, navigate_to_obj_sampler)
        nsrts.add(navigatetoball_nsrt)

        # NavigateToCup
        parameters = [robot, cup]
        option_vars = parameters
        option = NavigateToCup
        preconditions = set()
        add_effects = {ReachableCup([robot, cup])}
        ignore_effects = {ReachableSurface, ReachableBall, ReachableCube, ReachableCup}
        navigatetocup_nsrt = NSRT("NavigateToCup", parameters, preconditions,
                                  add_effects, set(), ignore_effects, option,
                                  option_vars, navigate_to_obj_sampler)
        nsrts.add(navigatetocup_nsrt)

        # NavigateToTable
        parameters = [robot, table]
        option_vars = parameters
        option = NavigateToTable
        preconditions = set()
        add_effects = {ReachableSurface([robot, table])}
        ignore_effects = {ReachableSurface, ReachableBall, ReachableCube, ReachableCup}
        navigatetotable_nsrt = NSRT("NavigateToTable",
                                    parameters, preconditions, add_effects,
                                    set(), ignore_effects, option, option_vars,
                                    navigate_to_obj_sampler)
        nsrts.add(navigatetotable_nsrt)

        return nsrts
