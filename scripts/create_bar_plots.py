"""Create bar plots."""

import os

import matplotlib
import matplotlib.pyplot as plt
import pandas as pd
plt.style.use('ggplot')

from predicators.scripts.analyze_results_directory import create_dataframes, \
    get_df_for_entry, pd_create_equal_selector, combine_selectors

plt.style.use('ggplot')
pd.options.mode.chained_assignment = None  # default='warn'
# plt.rcParams["font.family"] = "CMU Serif"

############################ Change below here ################################

# Details about the plt figure.
DPI = 500
FONT_SIZE = 18
X_LIM = (-5, 110)

# Groups over which to take mean/std.
GROUPS = [
    "ENV",
    "APPROACH",
    "EXCLUDED_PREDICATES",
    "EXPERIMENT_ID",
    # "NUM_TRAIN_TASKS", "CYCLE"
]

# All column names and keys to load into the pandas tables.
COLUMN_NAMES_AND_KEYS = [
    ("ENV", "env"),
    ("APPROACH", "approach"),
    ("EXCLUDED_PREDICATES", "excluded_predicates"),
    ("EXPERIMENT_ID", "experiment_id"),
    ("SEED", "seed"),
    # ("NUM_TRAIN_TASKS", "num_train_tasks"),
    # ("CYCLE", "cycle"),
    # ("NUM_SOLVED", "num_solved"),
    # ("AVG_NUM_PREDS", "avg_num_preds"),
    ("AVG_TEST_TIME", "avg_suc_time"),
    ("AVG_NODES_CREATED", "avg_num_nodes_created"),
    ("LEARNING_TIME", "learning_time"),
    ("PERC_SOLVED", "perc_solved"),
    # ("AVG_SKELETONS", "avg_num_skeletons_optimized"),
    # ("MIN_SKELETONS", "min_skeletons_optimized"),
    # ("MAX_SKELETONS", "max_skeletons_optimized"),
    # ("AVG_NODES_EXPANDED", "avg_num_nodes_expanded"),
    # ("AVG_NUM_NSRTS", "avg_num_nsrts"),
    # ("AVG_DISCOVERED_FAILURES", "avg_num_failures_discovered"),
    # ("AVG_PLAN_LEN", "avg_plan_length"),
    # ("AVG_EXECUTION_FAILURES", "avg_execution_failures"),
    # ("NUM_TRANSITIONS", "num_transitions"),
]

DERIVED_KEYS = [("perc_solved",
                 lambda r: 100 * r["num_solved"] / r["num_test_tasks"])]

KEY_AND_LABEL = [
    ("PERC_SOLVED", "% Evaluation Tasks Solved"),
    # ("AVG_NODES_CREATED", "Averaged nodes created"),
]

# The keys of the dict are (df key, df value), and the dict values are
# labels for the legend. The df key/value are used to select a subset from
# the overall pandas dataframe.
PLOT_GROUPS = [
    ("PickPlace1D", pd_create_equal_selector("ENV", "cover")),
    ("Blocks", pd_create_equal_selector("ENV", "pybullet_blocks")),
    ("Painting", pd_create_equal_selector("ENV", "painting")),
    ("Tools", pd_create_equal_selector("ENV", "tools")),
]

# See PLOT_GROUPS comment.
BAR_GROUPS = [
    ("Ours", lambda df: df["EXPERIMENT_ID"].apply(lambda v: "_main_200" in v)),
    ("Bisimulation",
     lambda df: df["EXPERIMENT_ID"].apply(lambda v: "_prederror_200" in v)),
    ("Branching",
     lambda df: df["EXPERIMENT_ID"].apply(lambda v: "_branchfac_200" in v)),
    ("Boltzmann",
     lambda df: df["EXPERIMENT_ID"].apply(lambda v: "_energy_200" in v)),
    ("GNN Shooting", lambda df: df["EXPERIMENT_ID"].apply(lambda v: "_gnn_shooting_200" in v)),
    ("GNN Model-Free", lambda df: df["EXPERIMENT_ID"].apply(lambda v: "_gnn_modelfree_200" in v)),
    ("Random", pd_create_equal_selector("APPROACH", "random_options")),
]

#################### Should not need to change below here #####################


def _main() -> None:
    outdir = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                          "results")
    os.makedirs(outdir, exist_ok=True)
    matplotlib.rcParams.update({'font.size': FONT_SIZE})

    grouped_means, grouped_stds, grouped_sizes = create_dataframes(
        COLUMN_NAMES_AND_KEYS, GROUPS, DERIVED_KEYS)
    means = grouped_means.reset_index()
    stds = grouped_stds.reset_index()
    sizes = grouped_sizes.reset_index().rename(columns={0: "SIZE"})

    for key, label in KEY_AND_LABEL:
        for plot_title, plot_selector in PLOT_GROUPS:
            _, ax = plt.subplots()
            plot_labels = []
            plot_means = []
            plot_stds = []
            for label, bar_selector in BAR_GROUPS:
                selector = combine_selectors([plot_selector, bar_selector])
                exp_means = get_df_for_entry(key, means, selector)
                exp_stds = get_df_for_entry(key, stds, selector)
                mean = exp_means[key].tolist()
                std = exp_stds[key].tolist()
                assert len(mean) == len(std) == 1
                plot_labels.append(label)
                plot_means.append(mean[0])
                plot_stds.append(std[0])
            ax.barh(plot_labels, plot_means, xerr=plot_stds)
            ax.set_xlim(X_LIM)
            ax.set_title(plot_title)
            plt.gca().invert_yaxis()
            plt.tight_layout()
            filename = f"{plot_title}_{key}.png"
            filename = filename.replace(" ", "_").lower()
            outfile = os.path.join(outdir, filename)
            plt.savefig(outfile, dpi=DPI)
            print(f"Wrote out to {outfile}.")


if __name__ == "__main__":
    _main()
