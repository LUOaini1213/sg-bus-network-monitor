"""A small spatio-temporal graph neural net for speed-band forecasting (PyTorch).

Design (Graph WaveNet / DCRNN family, kept small for a 4 GB GPU and a few days of data):
- node input: the last LAGS speeds (standardised, missing values filled with the segment's training mean, plus
  missing-value flags), time of day of the target-free anchor (sin, cos) and a weekday flag;
- an MLP lifts each node's input window to HIDDEN features (the temporal part: the whole one-hour window at once);
- LAYERS diffusion graph convolutions, each mixing K hops along the road direction (downstream), against it
  (upstream) and over a learned adaptive adjacency built from node embeddings, with residual connections;
- a linear head predicts, for each horizon, the change from the last observed speed. Predicting the change keeps
  persistence as the zero point the network has to improve on.
Loss: mean absolute error on observed targets. Early stopping on validation MAE; several seeds.
"""
import numpy as np
import torch
from torch import nn

HIDDEN, LAYERS, K_HOPS, EMB = 64, 2, 2, 16


def transition(src, dst, n, device):
    """Row-normalised (random-walk) sparse transition matrix for edges src -> dst."""
    if len(src) == 0:
        return torch.sparse_coo_tensor(torch.zeros((2, 0), dtype=torch.long), torch.zeros(0), (n, n)).to(device)
    deg = np.bincount(src, minlength=n).astype(np.float32)
    val = 1.0 / deg[src]
    i = torch.tensor(np.vstack([src, dst]), dtype=torch.long)
    return torch.sparse_coo_tensor(i, torch.tensor(val, dtype=torch.float32), (n, n)).coalesce().to(device)


class DiffusionConv(nn.Module):
    def __init__(self, dim, n_supports, k=K_HOPS, dropout=0.1):
        super().__init__()
        self.k = k
        self.lin = nn.Linear(dim * (1 + n_supports * k), dim)
        self.drop = nn.Dropout(dropout)

    def forward(self, h, supports):  # h: [B, N, D]
        b, n, d = h.shape
        outs = [h]
        for a in supports:
            x = h.permute(1, 0, 2).reshape(n, b * d)
            for _ in range(self.k):
                x = torch.sparse.mm(a, x) if a.is_sparse else a @ x
                outs.append(x.reshape(n, b, d).permute(1, 0, 2))
        return self.drop(self.lin(torch.cat(outs, dim=-1)))


class SpeedGNN(nn.Module):
    def __init__(self, n_nodes, in_dim, n_horizons, hidden=HIDDEN, layers=LAYERS, emb=EMB, adaptive=True):
        super().__init__()
        self.inp = nn.Sequential(nn.Linear(in_dim, hidden), nn.ReLU(), nn.Linear(hidden, hidden))
        self.adaptive = adaptive
        n_supports = 2 + int(adaptive)
        if adaptive:
            self.e1 = nn.Parameter(torch.randn(n_nodes, emb) * 0.1)
            self.e2 = nn.Parameter(torch.randn(n_nodes, emb) * 0.1)
        self.convs = nn.ModuleList([DiffusionConv(hidden, n_supports) for _ in range(layers)])
        self.norms = nn.ModuleList([nn.LayerNorm(hidden) for _ in range(layers)])
        self.head = nn.Sequential(nn.ReLU(), nn.Linear(hidden, n_horizons))

    def forward(self, x, fwd, bwd):
        h = self.inp(x)
        supports = [fwd, bwd]
        if self.adaptive:
            supports.append(torch.softmax(torch.relu(self.e1 @ self.e2.T), dim=1))
        for conv, norm in zip(self.convs, self.norms):
            h = norm(h + torch.relu(conv(h, supports)))
        return self.head(h)


def node_inputs(X, tf, idx, lags, mean, scale, link_mean):
    """[len(idx), N, 2*lags + 3] standardised input windows with missing flags and anchor time features."""
    w = np.stack([X[idx - k] for k in range(lags)], axis=-1)  # [B, N, lags], lag 0 first
    miss = ~np.isfinite(w)
    w = np.where(miss, link_mean[None, :, None], w)
    t = np.repeat(tf[idx][:, None, :], X.shape[1], axis=1)
    return np.concatenate([(w - mean) / scale, miss.astype(np.float32), t], axis=-1).astype(np.float32)


def train_and_predict(X, grid, tf, src, dst, train, val, test, link_mean, horizons=(1, 2, 3), lags=3,
                      seeds=(0, 1, 2, 3, 4), epochs=200, patience=20, batch=8, lr=2e-3, device=None, verbose=True):
    device = device or ("cuda" if torch.cuda.is_available() else "cpu")
    n = X.shape[1]
    rows = np.unique(np.concatenate([train - k for k in range(lags)]))
    vals = X[rows][np.isfinite(X[rows])]
    mean, scale = float(vals.mean()), float(vals.std())
    fwd = transition(src, dst, n, device)
    bwd = transition(dst, src, n, device)

    def tensors(idx):
        x = torch.tensor(node_inputs(X, tf, idx, lags, mean, scale, link_mean))
        last = np.where(np.isfinite(X[idx]), X[idx], link_mean[None])
        y = np.stack([X[idx + h] for h in horizons], axis=-1)
        return x, torch.tensor(last, dtype=torch.float32), torch.tensor(y, dtype=torch.float32)

    data = {k: tensors(v) for k, v in {"train": train, "val": val, "test": test}.items()}

    def predict(model, part):
        x, last, _ = data[part]
        out = []
        model.eval()
        with torch.no_grad():
            for i in range(0, len(x), batch):
                d = model(x[i:i + batch].to(device), fwd, bwd).cpu()
                out.append(last[i:i + batch, :, None] + d * scale)
        return torch.cat(out).numpy()

    def masked_mae(pred, y):
        ok = torch.isfinite(y)
        return (pred[ok] - y[ok]).abs().mean()

    per_seed, info = {}, {"device": device, "hidden": HIDDEN, "layers": LAYERS, "k_hops": K_HOPS, "seeds": {}}
    for seed in seeds:
        torch.manual_seed(seed)
        np.random.seed(seed)
        model = SpeedGNN(n, data["train"][0].shape[-1], len(horizons)).to(device)
        opt = torch.optim.Adam(model.parameters(), lr=lr, weight_decay=1e-5)
        best, best_state, bad, best_epoch = float("inf"), None, 0, 0
        x, last, y = data["train"]
        rng = np.random.default_rng(seed)
        for epoch in range(epochs):
            model.train()
            for b in np.array_split(rng.permutation(len(x)), max(1, len(x) // batch)):
                pred = last[b, :, None].to(device) + model(x[b].to(device), fwd, bwd) * scale
                loss = masked_mae(pred, y[b].to(device))
                opt.zero_grad()
                loss.backward()
                nn.utils.clip_grad_norm_(model.parameters(), 5.0)
                opt.step()
            pv = predict(model, "val")
            yv = data["val"][2].numpy()
            ok = np.isfinite(yv)
            v = float(np.abs(pv[ok] - yv[ok]).mean())
            if v < best - 1e-4:
                best, bad, best_epoch = v, 0, epoch
                best_state = {k: t.detach().clone() for k, t in model.state_dict().items()}
            else:
                bad += 1
                if bad >= patience:
                    break
        model.load_state_dict(best_state)
        p = predict(model, "test")
        per_seed[seed] = {h: p[..., j] for j, h in enumerate(horizons)}
        info["seeds"][seed] = {"best_epoch": best_epoch, "val_mae": round(best, 4)}
        if verbose:
            print(f"gnn seed {seed}: best epoch {best_epoch}, validation MAE {best:.3f}", flush=True)
    mean_pred = {h: np.mean([per_seed[s][h] for s in per_seed], axis=0) for h in horizons}
    info["parameters"] = int(sum(p.numel() for p in model.parameters()))
    return {"mean": mean_pred, "per_seed": per_seed}, info
