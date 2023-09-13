"""Interface for spot grasping skill."""

import logging
import time
from typing import Tuple

from bosdyn.api import geometry_pb2, manipulation_api_pb2
from bosdyn.client.manipulation_api_client import ManipulationApiClient
from bosdyn.client.sdk import Robot

from predicators.spot_utils.perception.perception_structs import \
    RGBDImageWithContext
from predicators.spot_utils.utils import stow_arm


def grasp_at_pixel(robot: Robot,
                   rgbd: RGBDImageWithContext,
                   pixel: Tuple[int, int],
                   move_while_grasping: bool = True,
                   timeout: float = 20.0) -> None:
    """Grasp an object at a specified pixel in the RGBD image, which should be
    from the hand camera and should be up to date with the robot's state.

    The `move_while_grasping` param dictates whether we're allowing the
    robot to automatically move its feet while grasping or not.
    """
    assert rgbd.camera_name == "hand_color_image"

    manipulation_client = robot.ensure_client(
        ManipulationApiClient.default_service_name)

    if move_while_grasping:
        # Stow Arm first (only if robot is allowed to move while grasping)
        stow_arm(robot)

    pick_vec = geometry_pb2.Vec2(x=pixel[0], y=pixel[1])

    # Build the proto. Note that the possible settings for walk_gaze_mode
    # can be found here:
    # https://dev.bostondynamics.com/protos/bosdyn/api/proto_reference.html
    walk_gaze_mode = 1 if move_while_grasping else 2
    grasp = manipulation_api_pb2.PickObjectInImage(
        pixel_xy=pick_vec,
        transforms_snapshot_for_camera=rgbd.transforms_snapshot,
        frame_name_image_sensor=rgbd.frame_name_image_sensor,
        camera_model=rgbd.camera_model,
        walk_gaze_mode=walk_gaze_mode)

    # Create the request.
    grasp_request = manipulation_api_pb2.ManipulationApiRequest(
        pick_object_in_image=grasp)

    # Send the request.
    cmd_response = manipulation_client.manipulation_api_command(
        manipulation_api_request=grasp_request)

    # Get feedback from the robot and execute grasping, repeating until a
    # proper response is received.
    start_time = time.perf_counter()
    while (time.perf_counter() - start_time) <= timeout:
        feedback_request = manipulation_api_pb2.ManipulationApiFeedbackRequest(
            manipulation_cmd_id=cmd_response.manipulation_cmd_id)
        response = manipulation_client.manipulation_api_feedback_command(
            manipulation_api_feedback_request=feedback_request)
        if response.current_state in [
                manipulation_api_pb2.MANIP_STATE_GRASP_SUCCEEDED,
                manipulation_api_pb2.MANIP_STATE_GRASP_FAILED
        ]:
            break
    if (time.perf_counter() - start_time) > timeout:
        logging.warning("Timed out waiting for grasp to execute!")


if __name__ == "__main__":
    # Run this file alone to test manually.
    # Make sure to pass in --spot_robot_ip.

    # pylint: disable=ungrouped-imports
    from pathlib import Path

    from bosdyn.client import create_standard_sdk
    from bosdyn.client.lease import LeaseClient, LeaseKeepAlive
    from bosdyn.client.util import authenticate

    from predicators import utils
    from predicators.settings import CFG
    from predicators.spot_utils.perception.spot_cameras import capture_images
    from predicators.spot_utils.spot_localization import SpotLocalizer
    from predicators.spot_utils.utils import get_pixel_from_user, verify_estop

    def _run_manual_test() -> None:
        # Put inside a function to avoid variable scoping issues.
        args = utils.parse_args(env_required=False,
                                seed_required=False,
                                approach_required=False)
        utils.update_config(args)

        # Get constants.
        hostname = CFG.spot_robot_ip
        upload_dir = Path(__file__).parent.parent / "graph_nav_maps"
        path = upload_dir / CFG.spot_graph_nav_map

        sdk = create_standard_sdk('NavigationSkillTestClient')
        robot = sdk.create_robot(hostname)
        authenticate(robot)
        verify_estop(robot)
        lease_client = robot.ensure_client(LeaseClient.default_service_name)
        lease_client.take()
        lease_keepalive = LeaseKeepAlive(lease_client,
                                         must_acquire=True,
                                         return_at_exit=True)
        robot.time_sync.wait_for_sync()
        localizer = SpotLocalizer(robot, path, lease_client, lease_keepalive)

        # Capture an image.
        camera = "hand_color_image"
        rgbd = capture_images(robot, localizer, [camera])[camera]

        # Select a pixel manually.
        pixel = get_pixel_from_user(rgbd.rgb)

        # Grasp at the pixel.
        grasp_at_pixel(robot, rgbd, pixel)

    _run_manual_test()
