from functools import partial
from pathlib import Path
from typing import List, Tuple

import matplotlib.pyplot as plt
import numpy as np
from jax.scipy.stats import beta as beta_distribution
from numpy.typing import NDArray
from scipy.optimize import minimize

from predicators import utils


def _run_inference(history: List[List[bool]],
                   betas: List[Tuple[float, float]]) -> List[float]:
    assert len(history) == len(betas)
    map_competences: List[float] = []
    # NOTE: this is the mean rather than the mode, for simplicity...
    # TODO: maybe change
    for outcomes, (a, b) in zip(history, betas):
        n = len(outcomes)
        s = sum(outcomes)
        alpha_n = a + s
        beta_n = n - s + b
        mean = alpha_n / (alpha_n + beta_n)
        assert 0 < mean < 1
        map_competences.append(mean)
    return map_competences


def _run_learning(
        num_data_before_cycle: NDArray[np.float32],
        map_competences: List[float]) -> Tuple[NDArray[np.float32], float]:
    """Return parameters for mean prediction and constant variance."""
    fn = partial(_loss, num_data_before_cycle, map_competences)
    # Transform into unconstrained space.
    theta_0 = np.array([0.25, 0.75, 1.0])
    unconstrained_theta_0 = _transform_model_params_to_unconstrain(theta_0)
    res = minimize(fn,
                   unconstrained_theta_0,
                   method="L-BFGS-B",
                   options=dict(maxiter=1000000, ftol=1e-1, eps=1e-3))
    unconstrained_theta_final = res.x
    theta_final = _invert_transform_model_params(unconstrained_theta_final)
    means_final = _model_predict(num_data_before_cycle, theta_final)
    variance_final = np.var(map_competences - means_final)
    return theta_final, variance_final


def _validate_model_params(theta: NDArray[np.float32]) -> None:
    theta0, theta1, theta2 = theta
    assert 0 <= theta0 <= 1
    assert theta0 <= theta1 <= 1
    assert theta2 >= 0


def _transform_model_params_to_unconstrain(
        theta: NDArray[np.float32]) -> NDArray[np.float32]:
    _validate_model_params(theta)
    theta0, theta1, theta2 = theta
    unconstrained_theta0 = np.log(theta0) - np.log(1 - theta0)  # logit
    unconstrained_theta1 = np.log(theta1) - np.log(
        1 - theta1)  # logit, will clip
    unconstrained_theta2 = theta2  # will clip
    return np.array(
        [unconstrained_theta0, unconstrained_theta1, unconstrained_theta2])


def _invert_transform_model_params(
        transformed_theta: NDArray[np.float32]) -> NDArray[np.float32]:
    utheta0, utheta1, utheta2 = transformed_theta
    theta0 = 1 / (1 + np.exp(-utheta0))  # sigmoid, inverse of logit
    theta1 = max(1 / (1 + np.exp(-utheta1)), theta0)  # sigmoid + clip
    theta2 = max(0, utheta2)  # clip
    theta = np.array([theta0, theta1, theta2], dtype=np.float32)
    _validate_model_params(theta)
    return theta


def _loss(cp_inputs: NDArray[np.float32], map_competences: List[float],
          model_params: NDArray[np.float32]) -> float:
    means = _model_predict(cp_inputs, model_params)
    variance = np.var(map_competences - means)
    betas = [_beta_from_mean_and_variance(m, variance) for m in means]
    nlls = [
        -beta_distribution.logpdf(c, a, b)
        for c, (a, b) in zip(map_competences, betas)
    ]
    return sum(nlls)


def _model_predict(
        x: NDArray[np.float32],
        transformed_params: NDArray[np.float32]) -> NDArray[np.float32]:
    params = _invert_transform_model_params(transformed_params)
    _validate_model_params(params)
    theta0, theta1, theta2 = params
    out = theta0 + (theta1 - theta0) * (1 - np.exp(-theta2 * x))
    assert np.all(out >= 0) and np.all(out <= 1)
    return out


def _beta_from_mean_and_variance(mean: float,
                                 variance: float) -> Tuple[float, float]:
    alpha = ((1 - mean) / variance - 1 / mean) * (mean**2)
    beta = alpha * (1 / mean - 1)
    return (alpha, beta)


def _get_cp_model_inputs(history: List[List[bool]]) -> NDArray[np.float32]:
    num_data_after_cycle = list(np.cumsum([len(h) for h in history]))
    num_data_before_cycle = np.array([0] + num_data_after_cycle[:-1],
                                     dtype=np.float32)
    return num_data_before_cycle


def _run_em(
    history: List[List[bool]],
    num_em_iters: int = 10
) -> Tuple[List[NDArray[np.float32]], List[Tuple[float, float]], List[float]]:
    num_cycles = len(history)
    cp_inputs = _get_cp_model_inputs(history)
    # Initialize betas with uniform distribution.
    betas = [(1.0, 1.0) for _ in range(num_cycles)]
    all_map_competences = []
    all_model_params = []
    all_betas = []
    for it in range(num_em_iters):
        print(f"Starting EM cycle {it}")
        # Run inference.
        map_competences = _run_inference(history, betas)
        print("MAP competences:", map_competences)
        all_map_competences.append(map_competences)
        # Run learning.
        model_params, variance = _run_learning(cp_inputs, map_competences)
        print("Model params:", model_params)
        print("Model variance:", variance)
        all_model_params.append(variance)
        # Update betas by evaluating the model.
        means = _model_predict(cp_inputs, model_params)
        betas = [_beta_from_mean_and_variance(m, variance) for m in means]
        print("Betas:", betas)
        all_betas.append(betas)
    return all_model_params, all_betas, all_map_competences


def _make_plots(history: List[List[bool]], all_betas: List[Tuple[float,
                                                                 float]],
                all_map_competences: List[float], outfile: Path) -> None:
    imgs: List[NDArray[np.uint8]] = []
    cp_inputs = _get_cp_model_inputs(history)
    for em_iter, (betas, map_competences) in enumerate(
            zip(all_betas, all_map_competences)):
        fig = plt.figure()
        plt.title(f"EM Iter {em_iter}")
        plt.xlabel("Skill Trial")
        plt.ylabel("Competence / Outcome")
        plt.xlim((min(cp_inputs) - 1, max(cp_inputs) + 1))
        plt.ylim((-0.25, 1.25))
        plt.yticks(np.linspace(0.0, 1.0, 5, endpoint=True))
        # Mark learning cycles.
        for i, x in enumerate(cp_inputs):
            label = "Learning Cycle" if i == 0 else None
            plt.plot((x, x), (-1.1, 2.1),
                     linestyle="--",
                     color="gray",
                     label=label)
        # Plot observation data.
        observations = [o for co in history for o in co]
        timesteps = np.arange(len(observations))
        plt.scatter(timesteps,
                    observations,
                    marker="o",
                    color="red",
                    label="Outcomes")
        # Plot competence progress model outputs (betas).
        means: List[float] = []
        stds: List[float] = []
        for a, b in betas:
            mean = a / (a + b)
            variance = (a * b) / ((a + b)**2 * (a + b + 1))
            std = np.sqrt(variance)
            means.append(mean)
            stds.append(std)
        plt.plot(cp_inputs, means, color="blue", marker="+", label="CP Model")
        lb = np.subtract(means, stds)
        plt.plot(cp_inputs, lb, color="blue", linestyle="--")
        ub = np.add(means, stds)
        plt.plot(cp_inputs, ub, color="blue", linestyle="--")
        # Plot MAP competences.
        for cycle, cycle_map_competence in enumerate(map_competences):
            label = "MAP Competence" if cycle == 0 else None
            x_start = cp_inputs[cycle]
            if cycle == len(map_competences) - 1:
                x_end = x_start  # just a point
            else:
                x_end = cp_inputs[cycle + 1]
            y = cycle_map_competence
            plt.plot((x_start, x_end), (y, y),
                     color="green",
                     marker="*",
                     label=label)
        # Finish figure.
        plt.legend(loc="center right", framealpha=1.0)
        img = utils.fig2data(fig, dpi=300)
        imgs.append(img)
    utils.save_video(outfile, imgs)


def _main():
    history = [
        [False, False, False],
        [True, False, False, True, False, False, False, False, False],
        [False, True, True, False, True, False, False, False],
        [False],
        [True, True, False, False, True, True],
        [True, True, True],
    ]
    all_model_params, all_betas, all_map_competences = _run_em(history)
    _make_plots(history,
                all_betas,
                all_map_competences,
                outfile=Path("cp_model_v1.mp4"))


if __name__ == "__main__":
    _main()
