import logging
from typing import Optional

import numpy as np
from scipy.linalg import LinAlgError, cho_factor, cho_solve, eig, eigh, lstsq, pinvh, qr
from scipy.sparse.linalg import eigs, eigsh
from sklearn.utils.extmath import randomized_svd

from kooplearn._src.linalg import _rank_reveal, modified_QR, weighted_norm
from kooplearn._src.utils import fuzzy_parse_complex, topk

logger = logging.getLogger("kooplearn")


def regularize(M: np.ndarray, reg: float, inplace=False):
    """Regularize a matrix by adding a multiple of the identity matrix to it.
    Args:
        M (np.ndarray): Matrix to regularize.
        reg (float): Regularization parameter.
    Returns:
        np.ndarray: Regularized matrix.
    """
    if inplace:
        return np.fill_diagonal(M, M.diagonal() + reg * M.shape[0])
    else:
        return M + (M.shape[0] * reg) * np.identity(M.shape[0], dtype=M.dtype)


def fit_reduced_rank_regression(
    kernel_X: np.ndarray,  # Kernel matrix of the input data
    kernel_Y: np.ndarray,  # Kernel matrix of the output data
    tikhonov_reg: float,  # Tikhonov regularization parameter, can be 0
    rank: int,  # Rank of the estimator
    svd_solver: str = "arnoldi",  # SVD solver to use. 'arnoldi' is faster but might be numerically unstable.
    _return_singular_values: bool = False
    # Whether to return the singular values of the projector. (Development purposes)
) -> tuple[np.ndarray, np.ndarray] or tuple[np.ndarray, np.ndarray, np.ndarray]:
    n_pts = kernel_X.shape[0]
    penalty = max(np.finfo(kernel_X.dtype).eps, tikhonov_reg)
    A = np.multiply(kernel_Y, n_pts ** (-0.5)) @ np.multiply(kernel_X, n_pts ** (-0.5))
    M = regularize(kernel_X, penalty)
    # Find U via Generalized eigenvalue problem equivalent to the SVD. If K is ill-conditioned might be slow.
    # Prefer svd_solver == 'randomized' in such a case.
    if svd_solver == "arnoldi":
        # Adding a small buffer to the Arnoldi-computed eigenvalues.
        _num_arnoldi_eigs = min(rank + 3, A.shape[0])
        sigma_sq, vecs = eigs(A, k=_num_arnoldi_eigs, M=M)
    else:  # 'full'
        sigma_sq, vecs = eig(A, M)
    # Filtering procedure.
    # First: Sort singular values by magnitude
    _, sort_perm = topk(np.abs(sigma_sq), len(sigma_sq))
    sigma_sq = sigma_sq[sort_perm]
    vecs = vecs[:, sort_perm]
    # Second: create a mask which is True when the real part of the eigenvalue is negative or the imaginary part is nonzero
    is_invalid = np.logical_or(np.real(sigma_sq) < 0, np.imag(sigma_sq) != 0)

    # Third, check if any is invalid take the first occurrence of a True value in the mask and filter everything after that
    if np.any(is_invalid):
        first_invalid = np.argmax(
            is_invalid
        )  # In the case of multiple occurrences of the maximum values, the indices corresponding to the first occurrence are returned.
        svals_filtered = sigma_sq[: min(first_invalid, rank)]
        vecs_filtered = vecs[:, : min(first_invalid, rank)]
        if first_invalid < rank:
            # Logging. Print the number of discarded eigenvalues, as well as the magnitude of the largest one.
            logger.warning(
                f"Warning: Discarted {len(sigma_sq) - len(svals_filtered)} dimensions of the {rank} requested, consider decreasing the rank. The largest squared singular value discarted is: {np.max(np.abs(sigma_sq[first_invalid:])):.3e}."
            )
    else:
        svals_filtered = sigma_sq[:rank]
        vecs_filtered = vecs[:, :rank]
    # Fourth assert that the eigenvectors do not have any imaginary part
    assert np.all(
        np.imag(vecs_filtered) == 0
    ), "The eigenvectors should be real. Decrease the rank or increase the regularization strength."
    # Fifth: take the real part of the eigenvectors
    vecs_filtered = np.real(vecs_filtered)
    svals_filtered = np.real(svals_filtered)
    # Sixth: compare the filtered eigenvalues with the regularization strength, and warn if there are any eigenvalues that are smaller than the regularization strength.
    if not np.all(np.abs(svals_filtered) >= tikhonov_reg):
        logger.warning(
            f"Warning: {(np.abs(svals_filtered) < tikhonov_reg).sum()} out of the {len(svals_filtered)} squared singular values are smaller than the regularization strength {tikhonov_reg:.2e}. Consider redudcing the regularization strength to avoid overfitting."
        )
    # Seventh: eigenvector normalization
    kernel_X_vecs = np.dot(np.multiply(kernel_X, n_pts ** (-0.5)), vecs_filtered)
    norm_sq = np.sum(
        kernel_X_vecs**2
        + tikhonov_reg * kernel_X_vecs * vecs_filtered * (n_pts**0.5),
        axis=0,
    ) ** (0.5)
    U = vecs_filtered / norm_sq
    V = kernel_X @ U
    if _return_singular_values:
        return U, V, sigma_sq
    else:
        return U, V


def fit_rand_reduced_rank_regression(
    K_X: np.ndarray,  # Kernel matrix of the input data
    K_Y: np.ndarray,  # Kernel matrix of the output data
    tikhonov_reg: float,  # Tikhonov regularization parameter
    rank: int,  # Rank of the estimator
    n_oversamples: int,  # Number of oversamples
    optimal_sketching: bool,  # Whether to use optimal sketching (slower but more accurate) or not.
    iterated_power: int,  # Number of iterations of the power method
    rng_seed: Optional[
        int
    ] = None,  # Seed for the random number generator (for reproducibility)
    _return_singular_values: bool = False
    # Whether to return the singular values of the projector. (Development purposes)
) -> tuple[np.ndarray, np.ndarray] or tuple[np.ndarray, np.ndarray, np.ndarray]:
    dim = K_X.shape[0]
    inv_dim = dim ** (-1.0)
    alpha = dim * tikhonov_reg
    tikhonov = np.identity(dim, dtype=K_X.dtype) * alpha
    K_reg = K_X + tikhonov
    c, low = cho_factor(K_reg)
    l = rank + n_oversamples
    rng = np.random.default_rng(rng_seed)
    if optimal_sketching:
        Cov = inv_dim * K_Y
        Om = rng.multivariate_normal(np.zeros(dim, dtype=K_X.dtype), Cov, size=l).T
    else:
        Om = rng.standard_normal(size=(dim, l))

    for _ in range(iterated_power):
        # Powered randomized rangefinder
        Om = (inv_dim * K_Y) @ (Om - alpha * cho_solve((c, low), Om))
        Om, _ = qr(Om, mode="economic")

    KOm = cho_solve((c, low), Om)
    KOmp = Om - alpha * KOm

    F_0 = Om.T @ KOmp
    F_1 = KOmp.T @ (inv_dim * (K_Y @ KOmp))

    # Generation of matrices U and V.
    try:
        sigma_sq, Q = eigh(F_1, F_0)
    except LinAlgError:
        sigma_sq, Q = eig(pinvh(F_0) @ F_1)

    Q_norm = np.sum(Q.conj() * (F_0 @ Q), axis=0)
    Q = Q * (Q_norm**-0.5)
    _idxs = topk(sigma_sq.real, rank).indices
    sigma_sq = sigma_sq.real

    Q = Q[:, _idxs]
    U = (dim**0.5) * np.asfortranarray(KOm @ Q)
    V = (dim**0.5) * np.asfortranarray(KOmp @ Q)
    if _return_singular_values:
        return U.real, V.real, sigma_sq
    else:
        return U.real, V.real


def fit_principal_component_regression(
    K_X: np.ndarray,  # Kernel matrix of the input data
    tikhonov_reg: float = 0.0,  # Tikhonov regularization parameter, can be zero
    rank: Optional[int] = None,  # Rank of the estimator
    svd_solver: str = "arnoldi",  # Solver for the generalized eigenvalue problem. 'arnoldi' or 'full'
) -> tuple[np.ndarray, np.ndarray]:
    dim = K_X.shape[0]

    if rank is None:
        rank = dim
    assert rank <= dim, f"Rank too high. The maximum value for this problem is {dim}"
    reg_K_X = regularize(K_X, tikhonov_reg)
    if svd_solver == "arnoldi":
        _num_arnoldi_eigs = min(rank + 3, reg_K_X.shape[0])
        values, vectors = eigsh(reg_K_X, k=_num_arnoldi_eigs)
    elif svd_solver == "full":
        values, vectors = eigh(reg_K_X)
    else:
        raise ValueError(f"Unknown svd_solver {svd_solver}")
    vectors, values, rsqrt_values = _rank_reveal(values, vectors, rank)
    vectors = np.sqrt(dim) * vectors * (rsqrt_values)
    return vectors, vectors


def fit_nystroem_reduced_rank_regression(
    kernel_X: np.ndarray,  # Kernel matrix of the input inducing points
    kernel_Y: np.ndarray,  # Kernel matrix of the output inducing points
    kernel_Xnys: np.ndarray,  # Kernel matrix between the input data and the input inducing points
    kernel_Ynys: np.ndarray,  # Kernel matrix between the output data and the output inducing points
    tikhonov_reg: float = 0.0,  # Tikhonov regularization parameter (can be 0)
    rank: Optional[int] = None,  # Rank of the estimator
    svd_solver: str = "arnoldi",  # Solver for the generalized eigenvalue problem. 'arnoldi' or 'full'
    _return_singular_values: bool = False
    # Whether to return the singular values of the projector. (Development purposes)
) -> tuple[np.ndarray, np.ndarray]:
    dim = kernel_X.shape[0]
    eps = kernel_X.shape[0] * np.finfo(kernel_X.dtype).eps
    reg = max(eps, tikhonov_reg)

    # LHS of the generalized eigenvalue problem
    kernel_YX_nys = kernel_Ynys.T @ kernel_Xnys

    _tmp_YX = lstsq(kernel_Y, kernel_YX_nys)[0]
    kernel_XYX = kernel_YX_nys.T @ _tmp_YX
    kernel_XYX = (kernel_XYX + kernel_XYX.T) * 0.5  # Symmetrize for numerical stability

    # RHS of the generalized eigenvalue problem
    kernel_Xnys_sq = kernel_Xnys.T @ kernel_Xnys + reg * dim * kernel_X

    if svd_solver == "full":
        values, vectors = eigh(
            kernel_XYX, b=regularize(kernel_Xnys_sq, eps)
        )  # normalization leads to needing to invert evals
    elif svd_solver == "arnoldi":
        _num_arnoldi_eigs = min(rank + 3, kernel_X.shape[0])
        values, vectors = eigsh(
            kernel_XYX,
            M=regularize(kernel_Xnys_sq, eps),
            k=_num_arnoldi_eigs,
            which="LM",
        )
    else:
        raise ValueError(f"Unknown svd_solver {svd_solver}")

    vectors, _, columns_permutation = modified_QR(
        vectors, M=kernel_XYX, column_pivoting=True
    )
    vectors = vectors[:, np.argsort(columns_permutation)]
    if vectors.shape[1] < rank:
        logger.warning(
            f"The numerical rank of the projector is smaller than the selected rank ({rank}). {rank - vectors.shape[1]} "
            f"degrees of freedom will be ignored."
        )
        _zeroes = np.zeros((vectors.shape[0], rank - vectors.shape[1]))
        vectors = np.c_[vectors, _zeroes]
        assert vectors.shape[1] == rank

    U = lstsq(kernel_Xnys_sq, kernel_XYX)[0] @ vectors
    V = _tmp_YX @ vectors

    if _return_singular_values:
        return U.real, V.real, values
    else:
        return U.real, V.real


def fit_nystroem_principal_component_regression(
    kernel_X: np.ndarray,  # Kernel matrix of the input inducing points
    kernel_Y: np.ndarray,  # Kernel matrix of the output inducing points
    kernel_Xnys: np.ndarray,  # Kernel matrix between the input data and the input inducing points
    kernel_Ynys: np.ndarray,  # Kernel matrix between the output data and the output inducing points
    tikhonov_reg: float = 0.0,  # Tikhonov regularization parameter (can be 0)
    rank: Optional[int] = None,  # Rank of the estimator
    svd_solver: str = "arnoldi",  # Solver for the generalized eigenvalue problem. 'arnoldi' or 'full'
) -> tuple[np.ndarray, np.ndarray]:
    dim = kernel_X.shape[0]
    eps = kernel_X.shape[0] * np.finfo(kernel_X.dtype).eps
    reg = max(eps, tikhonov_reg)
    kernel_Xnys_sq = kernel_Xnys.T @ kernel_Xnys
    if svd_solver == "full":
        values, vectors = eigh(
            kernel_Xnys_sq, regularize(kernel_X, reg)
        )  # normalization leads to needing to invert evals
    elif svd_solver == "arnoldi":
        _num_arnoldi_eigs = min(rank + 3, kernel_X.shape[0])
        values, vectors = eigsh(
            kernel_Xnys_sq,
            M=regularize(kernel_X, reg),
            k=_num_arnoldi_eigs,
            which="LM",
        )
    else:
        raise ValueError(f"Unknown svd_solver {svd_solver}")
    vectors, values, rsqrt_values = _rank_reveal(values, vectors, rank)

    U = np.sqrt(dim) * vectors * (rsqrt_values)
    V = np.linalg.multi_dot([kernel_Ynys.T, kernel_Xnys, vectors])
    V = lstsq(regularize(kernel_Y, eps), V)[0]
    V = np.sqrt(dim) * V * (rsqrt_values)
    return U, V


def fit_rand_principal_component_regression(
    K_X: np.ndarray,  # Kernel matrix of the input data
    tikhonov_reg: float,  # Tikhonov regularization parameter
    rank: int,  # Rank of the estimator
    n_oversamples: int,  # Number of oversamples
    iterated_power: int,  # Number of iterations for the power method
    rng_seed: Optional[int] = None,  # Seed for the random number generator
    _return_singular_values: bool = False
    # Whether to return the singular values of the projector. (Development purposes)
):
    dim = K_X.shape[0]
    vectors, values, _ = randomized_svd(
        regularize(K_X, tikhonov_reg),
        rank,
        n_oversamples=n_oversamples,
        n_iter=iterated_power,
        random_state=rng_seed,
    )
    vectors, values, rsqrt_values = _rank_reveal(values, vectors, rank)
    vectors = np.sqrt(dim) * vectors * (rsqrt_values)
    if _return_singular_values:
        return vectors, vectors, values
    else:
        return vectors, vectors


def predict(
    num_steps: int,  # Number of steps to predict (return the last one)
    U: np.ndarray,  # Projection matrix: first output of the fit functions defined above
    V: np.ndarray,  # Projection matrix: second output of the fit functions defined above
    K_YX: np.ndarray,  # Kernel matrix between the output data and the input data
    K_Xin_X: np.ndarray,  # Kernel matrix between the initial conditions and the input data
    obs_train_Y: np.ndarray,  # Observable to be predicted evaluated on the output training data
) -> np.ndarray:
    # G = S UV.T Z
    # G^n = (SU)(V.T K_YX U)^(n-1)(V.T Z)
    dim = U.shape[0]
    rsqrt_dim = dim ** (-0.5)
    K_dot_U = rsqrt_dim * K_Xin_X @ U
    V_dot_obs = rsqrt_dim * V.T @ obs_train_Y
    V_K_XY_U = (dim**-1) * np.linalg.multi_dot([V.T, K_YX, U])
    M = np.linalg.matrix_power(V_K_XY_U, num_steps - 1)
    return np.linalg.multi_dot([K_dot_U, M, V_dot_obs])


def estimator_eig(
    U: np.ndarray,  # Projection matrix: first output of the fit functions defined above
    V: np.ndarray,  # Projection matrix: second output of the fit functions defined above
    K_X: np.ndarray,  # Kernel matrix of the input data
    K_YX: np.ndarray,  # Kernel matrix between the output data and the input data
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    # SUV.TZ -> V.T K_YX U (right ev = SUvr, left ev = ZVvl)
    r_dim = (K_X.shape[0]) ** (-1)

    W_YX = np.linalg.multi_dot([V.T, r_dim * K_YX, U])
    W_X = np.linalg.multi_dot([U.T, r_dim * K_X, U])

    values, vl, vr = eig(W_YX, left=True, right=True)  # Left -> V, Right -> U
    values = fuzzy_parse_complex(values)
    r_perm = np.argsort(values)
    vr = vr[:, r_perm]
    l_perm = np.argsort(values.conj())
    vl = vl[:, l_perm]
    values = values[r_perm]

    # Normalization in RKHS
    norm_r = weighted_norm(vr, W_X)
    r_normr = np.where(norm_r == 0.0, 0.0, norm_r**-1)
    vr = vr * r_normr

    # Bi-orthogonality of left eigenfunctions
    norm_l = np.diag(np.linalg.multi_dot([vl.T, W_YX, vr]))
    r_norm_l = np.where(np.abs(norm_l) == 0, 0.0, norm_l**-1)
    vl = vl * r_norm_l
    return values, V @ vl, U @ vr


def estimator_modes(K_Xin_X: np.ndarray, rv: np.ndarray, lv: np.ndarray):
    r_dim = lv.shape[0] ** -0.5
    rv_in = evaluate_eigenfunction(K_Xin_X, rv).T  # [rank, num_initial_conditions]
    lv_obs = r_dim * lv.T  # [rank, num_observations]
    return (
        rv_in[:, :, None] * lv_obs[:, None, :]
    )  # [rank, num_init_conditions, num_training_points]


def evaluate_eigenfunction(
    K_Xin_X_or_Y: np.ndarray,
    # Kernel matrix between the initial conditions and the input data (right eigenfunctions) or the output data
    # (left eigenfunctions)
    vr_or_vl: np.ndarray,  # Right eigenvectors or left eigenvectors, as returned by the estimator_eig function
):
    rsqrt_dim = (K_Xin_X_or_Y.shape[1]) ** (-0.5)
    return np.linalg.multi_dot([rsqrt_dim * K_Xin_X_or_Y, vr_or_vl])


def svdvals(
    U: np.ndarray,  # Projection matrix: first output of the fit functions defined above
    V: np.ndarray,  # Projection matrix: second output of the fit functions defined above
    K_X: np.ndarray,  # Kernel matrix of the input data
    K_Y: np.ndarray,  # Kernel matrix of the output data
):
    # Inefficient implementation
    rdim = (K_X.shape[0]) ** (-1)
    A = np.linalg.multi_dot([V.T, rdim * K_Y, V])
    B = np.linalg.multi_dot([U.T, rdim * K_X, U])
    v = eig(A @ B, left=False, right=False)
    # Clip the negative values
    v = v.real
    v[v < 0] = 0
    return np.sqrt(v)


def estimator_risk(
    kernel_Yv: np.ndarray,  # Kernel matrix of the output validation data
    kernel_Y: np.ndarray,  # Kernel matrix of the output training data
    kernel_XXv: np.ndarray,  # Cross-Kernel matrix of the input train/validation data
    kernel_YYv: np.ndarray,  # Cross-Kernel matrix of the output train/validation data
    U: np.ndarray,  # Projection matrix: first output of the fit functions defined above
    V: np.ndarray,  # Projection matrix: second output of the fit functions defined above
):
    rdim_train = (kernel_Y.shape[0]) ** (-1)
    rdim_val = (kernel_Yv.shape[0]) ** (-1)

    r_Y = rdim_val * np.trace(kernel_Yv)
    r_XY = (
        -2
        * rdim_val
        * rdim_train
        * np.trace(np.linalg.multi_dot([kernel_YYv.T, V, U.T, kernel_XXv]))
    )
    r_X = (
        rdim_val
        * (rdim_train**2)
        * np.trace(
            np.linalg.multi_dot([kernel_XXv.T, U, V.T, kernel_Y, V, U.T, kernel_XXv])
        )
    )
    return r_Y + r_XY + r_X
