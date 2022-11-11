#!/bin/bash

DATA_DIR="blocks_vision_data"
TASK_NUM="0"
IMG_SUFFIX="821212060083.png"
TASK_DIR=$DATA_DIR/tasks
OUTPUT=$TASK_DIR/blocks-vision-task$TASK_NUM.json
VIZ_PLANNING="True"

mkdir -p $TASK_DIR

echo "Running perception."
python scripts/run_blocks_perception.py \
        --rgb $DATA_DIR/color-$TASK_NUM-$IMG_SUFFIX \
        --depth $DATA_DIR/depth-$TASK_NUM-$IMG_SUFFIX \
        --goal $DATA_DIR/goal-$TASK_NUM.json \
        --extrinsics $DATA_DIR/extrinsics.json \
        --intrinsics $DATA_DIR/intrinsics.json \
        --output $OUTPUT

echo "Running planning."
python predicators/main.py --env pybullet_blocks --approach oracle --seed 0 \
    --num_test_tasks 1 --blocks_test_task_json_dir $TASK_DIR \
    --pybullet_robot panda \
    --option_model_use_gui $VIZ_PLANNING \
    --option_model_name oracle --option_model_terminate_on_repeat False \
    --blocks_num_blocks_test [15] # just needs to be an upper bound

echo "Converting plan to LISDF."
python scripts/eval_trajectory_to_lisdf.py \
        --input eval_trajectories/pybullet_blocks__oracle__0________task1.traj \
        --output /tmp/pybullet_blocks__oracle__0________task1.json
