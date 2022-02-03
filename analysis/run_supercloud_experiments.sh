#!/bin/bash

START_SEED=456
NUM_SEEDS=50
FILE="analysis/submit.py"

for SEED in $(seq $START_SEED $((NUM_SEEDS+START_SEED-1))); do
    COMMON_ARGS="--seed $SEED"

    # tools
    python $FILE --experiment_id oracleall --env tools --approach oracle $COMMON_ARGS
    python $FILE --experiment_id learnall --env tools --approach nsrt_learning $COMMON_ARGS
    python $FILE --experiment_id oraclesamplers --env tools --approach nsrt_learning $COMMON_ARGS --sampler_learner oracle
done
