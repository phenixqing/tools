"""Black-Scholes model with Greeks and implied volatility."""
import numpy as np
from scipy.stats import norm
from scipy.optimize import brentq


def _d1_d2(S: float, K: float, T: float, r: float, sigma: float, q: float = 0.0):
    """Compute d1 and d2 for Black-Scholes with continuous dividend yield q."""
    d1 = (np.log(S / K) + (r - q + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    return d1, d2


def bs_price(S: float, K: float, T: float, r: float, sigma: float,
             q: float = 0.0, option_type: str = "call") -> float:
    """Black-Scholes option price (Merton model with dividend yield q)."""
    if T <= 0:
        if option_type == "call":
            return max(S - K, 0.0)
        return max(K - S, 0.0)

    d1, d2 = _d1_d2(S, K, T, r, sigma, q)
    disc_S = S * np.exp(-q * T)
    disc_K = K * np.exp(-r * T)

    if option_type == "call":
        return disc_S * norm.cdf(d1) - disc_K * norm.cdf(d2)
    return disc_K * norm.cdf(-d2) - disc_S * norm.cdf(-d1)


def bs_greeks(S: float, K: float, T: float, r: float, sigma: float,
              q: float = 0.0, option_type: str = "call") -> dict:
    """Return Black-Scholes option price and all first-order Greeks.

    Greeks:
        delta  – ∂V/∂S
        gamma  – ∂²V/∂S²
        theta  – ∂V/∂t  (per calendar day, negative for long options)
        vega   – ∂V/∂σ  (per 1 % point move in IV)
        rho    – ∂V/∂r  (per 1 % point move in rate)
    """
    if T <= 0:
        intrinsic = max(S - K, 0.0) if option_type == "call" else max(K - S, 0.0)
        return dict(price=intrinsic, delta=0.0, gamma=0.0, theta=0.0, vega=0.0, rho=0.0)

    d1, d2 = _d1_d2(S, K, T, r, sigma, q)
    n_d1 = norm.pdf(d1)
    disc_S = S * np.exp(-q * T)
    disc_K = K * np.exp(-r * T)

    price = bs_price(S, K, T, r, sigma, q, option_type)

    # Delta
    if option_type == "call":
        delta = np.exp(-q * T) * norm.cdf(d1)
    else:
        delta = np.exp(-q * T) * (norm.cdf(d1) - 1.0)

    # Gamma (same for call/put)
    gamma = np.exp(-q * T) * n_d1 / (S * sigma * np.sqrt(T))

    # Theta (per calendar day)
    common = -disc_S * n_d1 * sigma / (2.0 * np.sqrt(T))
    if option_type == "call":
        theta = (common + q * disc_S * norm.cdf(d1) - r * disc_K * norm.cdf(d2)) / 365.0
    else:
        theta = (common - q * disc_S * norm.cdf(-d1) + r * disc_K * norm.cdf(-d2)) / 365.0

    # Vega (per 1 % point in IV, i.e. Δσ=0.01)
    vega = disc_S * n_d1 * np.sqrt(T) * 0.01

    # Rho (per 1 % point in r)
    if option_type == "call":
        rho = disc_K * T * norm.cdf(d2) * 0.01
    else:
        rho = -disc_K * T * norm.cdf(-d2) * 0.01

    return dict(price=price, delta=delta, gamma=gamma, theta=theta, vega=vega, rho=rho)


def implied_vol(market_price: float, S: float, K: float, T: float,
                r: float, q: float = 0.0, option_type: str = "call",
                fallback: float = 0.40) -> float:
    """Solve for implied volatility via Brent's method.  Returns `fallback` on failure."""
    if T <= 0:
        return fallback

    intrinsic = max(S - K, 0.0) if option_type == "call" else max(K - S, 0.0)
    if market_price <= max(intrinsic, 1e-6):
        return fallback

    def objective(sigma):
        return bs_price(S, K, T, r, sigma, q, option_type) - market_price

    try:
        # Upper bound: very high vol – option price approaches max bound
        hi_price = bs_price(S, K, T, r, 5.0, q, option_type)
        if hi_price < market_price:
            return fallback
        iv = brentq(objective, 1e-4, 5.0, xtol=1e-7, maxiter=200)
        return float(iv)
    except Exception:
        return fallback
