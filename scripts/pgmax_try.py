from functools import partial

import jax
import jax.numpy as jnp
import matplotlib.pyplot as plt
import numpy as np
from pgmax import fgraph, fgroup, infer, vgroup
from scipy.special import logsumexp
from scipy.stats import bernoulli, beta, norm
from scipy.optimize import curve_fit
from predicators import utils
    


###############################################################################
#                                Inference                                    #
###############################################################################

# Quantizing.
NUM_QUANTS = 100

def _get_quant_centers(start=0.0, end=1.0, num_quants=NUM_QUANTS):
    lefts = np.linspace(start, end, num_quants, endpoint=False)
    delta = lefts[1] - lefts[0]
    return (lefts + delta / 2).squeeze()


def _quantize(fn, start=0.0, end=1.0, num_quants=NUM_QUANTS):
    centers = _get_quant_centers(start, end, num_quants)
    unnormed = np.vectorize(fn)(centers)
    z = logsumexp(unnormed)
    return unnormed - z


def _quant_to_value(quant, start=0.0, end=1.0, num_quants=NUM_QUANTS):
    centers = _get_quant_centers(start, end, num_quants)
    return centers[quant]


def _quantize2d(fn, start=0.0, end=1.0, num_quants=NUM_QUANTS):
    # TODO figure out way to unify this.
    unnormed = np.empty((num_quants, num_quants))
    centers0 = _get_quant_centers(start, end, num_quants)
    centers1 = _get_quant_centers(start, end, num_quants)
    for i, center0 in enumerate(centers0):
        for j, center1 in enumerate(centers1):
            unnormed[i, j] = fn(center0, center1)
    z = logsumexp(unnormed.flat)
    return unnormed - z


def run_inference(outcomes, model_params, model_sigma):

    num_cycles = len(outcomes)
    all_num_outcomes = [len(o) for o in outcomes]
    cum_num_outcomes = np.cumsum(all_num_outcomes)

    # Create variables and initialize factor graph.
    obs_variable_groups = []
    for cycle, cycle_outcomes in enumerate(outcomes):
        num_outcomes = len(cycle_outcomes)
        obs_cycle_group = vgroup.NDVarArray(num_states=2, shape=(num_outcomes, ))
        obs_variable_groups.append(obs_cycle_group)
    competence_variable_group = vgroup.NDVarArray(num_states=NUM_QUANTS,
                                                shape=(num_cycles, ))
    variable_groups = obs_variable_groups + [competence_variable_group]
    fg = fgraph.FactorGraph(variable_groups=variable_groups)

    # Create factors.
    factors = []

    # Create unary factors for observed values.
    for cycle, cycle_outcomes in enumerate(outcomes):
        num_outcomes = len(cycle_outcomes)
        visible_log_potentials = np.full((num_outcomes, 2), -np.inf)
        for i, out in enumerate(cycle_outcomes):
            visible_log_potentials[i, int(out)] = 0
        cycle_visible_factor = fgroup.EnumFactorGroup(
            variables_for_factors=[[obs_variable_groups[cycle][i]]
                                for i in range(num_outcomes)],
            factor_configs=np.arange(2)[:, None],
            log_potentials=visible_log_potentials,
        )
        factors.append(cycle_visible_factor)

    # Create observation model factors.
    def observation_log_potential(outcome, competence):
        return bernoulli.logpmf(outcome, competence)

    quantized_observation_log_potentials = np.array([
        _quantize(partial(observation_log_potential, False)),
        _quantize(partial(observation_log_potential, True)),
    ])

    for cycle, cycle_outcomes in enumerate(outcomes):
        num_outcomes = len(cycle_outcomes)
        cycle_observation_factor = fgroup.PairwiseFactorGroup(
            variables_for_factors=[[
                obs_variable_groups[cycle][i], competence_variable_group[cycle]
            ] for i in range(num_outcomes)],
            log_potential_matrix=quantized_observation_log_potentials,
        )
        factors.append(cycle_observation_factor)

    # Create competence progress factors.
    def create_competence_progress_log_potential(current_cycle):
        x = float(cum_num_outcomes[current_cycle])
        mu = parameterized_model_predict(x, *model_params)

        def competence_progress_log_potential(current_competence):
            return norm.logpdf(current_competence, loc=mu, scale=model_sigma)

        return competence_progress_log_potential

    a, b = 0.5, 0.5  # beta distribution parameters
    def initial_competence_log_potential(competence):
        return beta.logpdf(competence, a, b)

    for cycle in range(num_cycles - 1):
        current_competence_var = competence_variable_group[cycle]
        competence_progress_log_potential = create_competence_progress_log_potential(
            cycle)
        quantized_competence_progress_log_potential = _quantize(
            competence_progress_log_potential)
        
        # For the first time step, incorporate prior.
        if cycle == 0:
            quantized_competence_progress_log_potential += _quantize(
                initial_competence_log_potential)

        cycle_competence_factor = fgroup.EnumFactorGroup(
            variables_for_factors=[[current_competence_var]],
            factor_configs=np.arange(NUM_QUANTS)[:, None],
            log_potentials=quantized_competence_progress_log_potential,
        )
        factors.append(cycle_competence_factor)

    # Finalize factor graph.
    fg.add_factors(factors)

    # Run MAP inference.
    bp = infer.build_inferer(fg.bp_state, backend="bp")
    bp_arrays = bp.run(bp.init(), num_iters=100, damping=0.5, temperature=0.0)
    beliefs = bp.get_beliefs(bp_arrays)
    map_states = infer.decode_map_states(beliefs)
    map_competences = [
        _quant_to_value(map_states[competence_variable_group][i])
        for i in range(num_cycles)
    ]
    return map_competences


###############################################################################
#                                 Learning                                    #
###############################################################################

def parameterized_model_predict(x, a, b, c, d):
    # https://arxiv.org/pdf/2103.10948.pdf
    # POW3 model
    # mu = a * (x - d) ** (-b) + c
    # return 1.0 - np.exp(-mu)

    # Logistic function
    return a / (1 + np.exp(-b * (x - d))) + c

def get_init_model_params():
    a = 1
    b = 1
    c = 0
    d = 10
    sigma = 1
    return (np.array([a, b, c, d]), sigma)

def run_learning(cum_num_outcomes, map_competences, maxfev=1000000, num_restarts=5):
    rng = np.random.default_rng(0)
    x = np.array(cum_num_outcomes, dtype=np.float32)
    y = np.array(map_competences, dtype=np.float32)
    best_popt = None
    best_sigma = np.inf
    for i in range(num_restarts):
        p0 = rng.normal(size=4)
        popt, _ = curve_fit(parameterized_model_predict, x, y, maxfev=maxfev, p0=p0)
        yhat = parameterized_model_predict(x, *popt)
        err = (y - yhat)
        err_mean = err.mean()
        sigma = (err-err_mean).T @ (err-err_mean) / err.shape[0]
        print(f"[Learning] Restart {i} sigma: {sigma}")
        if sigma < best_sigma:
            best_sigma = sigma
            best_popt = popt
    print(f"[Learning] Learned parameters: {best_popt}")
    print(f"[Learning] Prediction variance: {best_sigma}")
    return best_popt, best_sigma

###############################################################################
#                                    EM                                       #
###############################################################################

def run_em(outcomes, num_iters=10):
    all_num_outcomes = [len(o) for o in outcomes]
    cum_num_outcomes = np.cumsum(all_num_outcomes)
    model_params, model_sigma = get_init_model_params()
    all_model_params = [
       (model_params.copy(), model_sigma)
    ]
    all_map_competences = []
    for it in range(num_iters):
        print(f"Starting EM iteration {it}...")
        map_competences = run_inference(outcomes, model_params, model_sigma)
        all_map_competences.append(map_competences)
        for cycle in range(len(outcomes)):
            print(f"[Inference] Competence {cycle}: {map_competences[cycle]}")
        model_params, model_sigma = run_learning(cum_num_outcomes, map_competences)
        all_model_params.append((model_params.copy(), model_sigma))
    map_competences = run_inference(outcomes, model_params, model_sigma)
    all_map_competences.append(map_competences)
    return all_model_params, all_map_competences

###############################################################################
#                                  Analysis                                   #
###############################################################################

def _make_plots(outcomes, all_model_params, all_map_competences, outfile = "pgmax_script_out.mp4"):
    imgs = []
    all_num_outcomes = [len(o) for o in outcomes]
    num_trials = sum(all_num_outcomes)
    cum_num_outcomes = np.cumsum(all_num_outcomes)
    for em_iter, (model_params, model_sigma) in enumerate(all_model_params):
        fig = plt.figure()
        plt.title(f"EM Iter {em_iter}")
        plt.xlabel("Skill Trial")
        plt.ylabel("Competence / Outcome")
        plt.xlim((-1, num_trials+1))
        plt.ylim((-0.25, 1.25))
        plt.yticks(np.linspace(0.0, 1.0, 5, endpoint=True))
        # Mark learning cycles.
        for i, x in enumerate(cum_num_outcomes):
            label = "Learning Cycle" if i == 0 else None
            plt.plot((x, x), (-1.1, 2.1), linestyle="--", color="gray", label=label)
        # Plot observation data.
        observations = [o for co in outcomes for o in co]
        timesteps = np.arange(len(observations))
        plt.scatter(timesteps, observations, marker="o", color="red", label="Outcomes")
        # Plot competence progress model.
        inputs = np.linspace(0, num_trials, 1000)
        outputs = parameterized_model_predict(inputs, *model_params)
        plt.plot(inputs, outputs, color="blue", label="CP Model")
        lb = outputs - model_sigma
        plt.plot(inputs, lb, color="blue", linestyle="--")
        ub = outputs + model_sigma
        plt.plot(inputs, ub, color="blue", linestyle="--")
        # Plot MAP competences.
        map_competences = all_map_competences[em_iter]
        for cycle, cycle_map_competence in enumerate(map_competences):
            label = "MAP Competence" if cycle == 0 else None
            x_start = 0 if cycle == 0 else cum_num_outcomes[cycle-1]
            x_end = cum_num_outcomes[cycle]
            y = cycle_map_competence
            plt.plot((x_start, x_end), (y, y), color="green", label=label)
        # Finish figure.
        plt.legend(loc="center right", framealpha=1.0)
        img = utils.fig2data(fig, dpi=300)
        imgs.append(img)
    utils.save_video(outfile, imgs)


if __name__ == "__main__":
    # data = [
    #     [False, False, False],
    #     [True, False, False, True, False, False, False, False, False],
    #     [False, True, True, False, True, False, False, False],
    #     [False],
    #     [True, True, False, False, True, True],
    #     [True, True, True],
    # ]
    # mp_out, map_out = run_em(data)
    # _make_plots(data, mp_out, map_out, outfile = "pgmax_script_out_v1.mp4")
    # data = [
    #     [False, False, False, False, False],
    #     [False, False, False, False],
    #     [False, False, False, False, False, False, False],
    #     [False, False],
    # ]
    # mp_out, map_out = run_em(data)
    # _make_plots(data, mp_out, map_out, outfile = "pgmax_script_out_v2.mp4")
    data = [
        [True, True, True, True, True],
        [True, True, True, True, True, True, True],
        [True, True, True, True, True],
        [True, True, True, True, True, True, True, True, True],
        [True, True, True, True, True],

    ]
    mp_out, map_out = run_em(data)
    _make_plots(data, mp_out, map_out, outfile = "pgmax_script_out_v3.mp4")
