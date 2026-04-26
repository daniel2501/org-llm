# [[file:../../../org/20260425230731-org_llm.org::*models.py][models.py:1]]
"""FOSS LLM catalog, hardware-aware tuning, and FOSS tool registry."""
from __future__ import annotations

from typing import NamedTuple


class ModelInfo(NamedTuple):
    tag:         str    # ollama pull tag
    license:     str    # SPDX or short name
    vram_gb:     float  # VRAM needed at 4-bit quant
    roles:       tuple  # subset of ROLE_KEYS
    params:      str    # human-readable size ("7B")
    description: str    # one-line summary


# ── Capability roles (maps to _TASK_MODEL_KEYS in cli.py) ─────────────────────
ROLE_KEYS = ("embed", "chat", "code", "reason", "fast", "instruct", "text")

# ── Curated FOSS / open-weights LLM catalog ───────────────────────────────────
# Sorted: embed → tiny → small → medium → code → reason → large
CATALOG: list[ModelInfo] = [
    # Embeddings
    ModelInfo("nomic-embed-text",         "Apache 2.0",       0.5, ("embed",),                       "0.3B",  "Fast, high-quality; the standard choice"),
    ModelInfo("mxbai-embed-large",        "Apache 2.0",       0.7, ("embed",),                       "0.3B",  "Slightly higher retrieval quality"),
    ModelInfo("bge-m3",                   "MIT",              0.7, ("embed",),                       "0.6B",  "Multilingual; FlagAI / BAAI"),
    ModelInfo("snowflake-arctic-embed2",  "Apache 2.0",       0.8, ("embed",),                       "0.6B",  "State-of-the-art retrieval benchmark"),
    # Sub-2B (fast / tagging)
    ModelInfo("qwen2.5:0.5b",             "Apache 2.0",       0.7, ("fast",),                        "0.5B",  "Near-instant classification; Alibaba"),
    ModelInfo("tinyllama:1.1b",           "Apache 2.0",       1.0, ("fast",),                        "1.1B",  "Runs on a potato"),
    ModelInfo("llama3.2:1b",              "Meta Llama 3",     1.5, ("fast", "chat"),                 "1B",    "Meta — surprisingly capable at 1B"),
    ModelInfo("smollm2:1.7b",             "Apache 2.0",       1.5, ("fast", "chat"),                 "1.7B",  "HuggingFace; top tiny model"),
    # 3-4B
    ModelInfo("llama3.2:3b",              "Meta Llama 3",     3.0, ("chat", "instruct"),             "3B",    "Meta; best open 3B model"),
    ModelInfo("phi3.5:3.8b",              "MIT",              3.0, ("chat", "instruct"),             "3.8B",  "Microsoft; exceptional efficiency"),
    ModelInfo("gemma3:4b",                "Gemma ToS",        3.5, ("chat", "instruct", "text"),     "4B",    "Google; very capable at 4B"),
    ModelInfo("qwen2.5:3b",               "Apache 2.0",       3.0, ("chat", "instruct"),             "3B",    "Alibaba; strong at 3B"),
    # 7-8B
    ModelInfo("mistral:7b",               "Apache 2.0",       5.5, ("chat", "instruct", "text"),     "7B",    "Mistral AI; classic workhorse"),
    ModelInfo("llama3.1:8b",              "Meta Llama 3",     6.0, ("chat", "instruct", "text"),     "8B",    "Meta; strong all-rounder"),
    ModelInfo("qwen2.5:7b",               "Apache 2.0",       5.5, ("chat", "instruct", "text"),     "7B",    "Alibaba; consistently strong"),
    ModelInfo("granite3.1-dense:8b",      "Apache 2.0",       6.0, ("chat", "instruct"),             "8B",    "IBM; enterprise-grade Apache 2.0"),
    ModelInfo("falcon:7b",                "Apache 2.0",       5.5, ("chat",),                        "7B",    "TII Abu Dhabi; fully open weights"),
    # Code
    ModelInfo("qwen2.5-coder:7b",         "Apache 2.0",       5.5, ("code",),                       "7B",    "Best open code model at 7B"),
    ModelInfo("deepseek-coder-v2:16b",    "MIT",             12.0, ("code",),                       "16B",   "DeepSeek; excellent all-round coder"),
    ModelInfo("qwen2.5-coder:32b",        "Apache 2.0",      22.0, ("code",),                       "32B",   "State-of-the-art open coding assistant"),
    ModelInfo("codellama:7b",             "Meta Llama 2",     5.5, ("code",),                       "7B",    "Meta's original code model"),
    ModelInfo("starcoder2:7b",            "BigCode OpenRAIL", 5.5, ("code",),                       "7B",    "Trained exclusively on permissive code"),
    # Reasoning
    ModelInfo("deepseek-r1:7b",           "MIT",              5.5, ("reason",),                     "7B",    "Chain-of-thought; efficient at 7B"),
    ModelInfo("deepseek-r1:14b",          "MIT",             10.0, ("reason",),                     "14B",   "Stronger reasoning; 14B"),
    ModelInfo("deepseek-r1:32b",          "MIT",             22.0, ("reason",),                     "32B",   "Excellent reasoning"),
    ModelInfo("deepseek-r1:70b",          "MIT",             48.0, ("reason",),                     "70B",   "Best open reasoning model"),
    ModelInfo("qwq:32b",                  "Apache 2.0",      22.0, ("reason",),                     "32B",   "Qwen reasoning; very strong"),
    # 12-14B
    ModelInfo("mistral-nemo:12b",         "Apache 2.0",       8.5, ("chat", "instruct", "text"),    "12B",   "Larger mistral; excellent quality"),
    ModelInfo("phi4:14b",                 "MIT",              9.0, ("chat", "instruct", "reason"),   "14B",   "Microsoft; exceptional at 14B"),
    ModelInfo("qwen2.5:14b",              "Apache 2.0",      10.0, ("chat", "instruct", "text"),    "14B",   "Alibaba; excellent mid-size"),
    # 27B+
    ModelInfo("gemma3:27b",               "Gemma ToS",       18.0, ("chat", "instruct", "text"),    "27B",   "Google; top open 27B"),
    ModelInfo("llama3.3:70b",             "Meta Llama 3",    48.0, ("chat", "instruct", "reason", "text"), "70B", "Best open chat; frontier quality"),
    ModelInfo("qwen2.5:72b",              "Apache 2.0",      48.0, ("chat", "instruct", "text"),    "72B",   "Frontier open model; Alibaba"),
]

# Quality rank within a role (higher = better) — used by the tuner
_QUALITY: dict[str, int] = {
    # embed
    "snowflake-arctic-embed2": 100, "mxbai-embed-large": 90, "bge-m3": 85, "nomic-embed-text": 80,
    # fast
    "smollm2:1.7b": 60, "llama3.2:1b": 55, "tinyllama:1.1b": 40, "qwen2.5:0.5b": 35,
    # chat / instruct / text
    "llama3.3:70b": 200, "qwen2.5:72b": 195, "gemma3:27b": 170,
    "phi4:14b": 150, "qwen2.5:14b": 145, "mistral-nemo:12b": 140,
    "llama3.1:8b": 120, "qwen2.5:7b": 115, "gemma3:4b": 112, "mistral:7b": 110,
    "granite3.1-dense:8b": 108, "llama3.2:3b": 100, "phi3.5:3.8b": 98, "qwen2.5:3b": 95,
    "falcon:7b": 88, "llama3.2:1b": 60,
    # code
    "qwen2.5-coder:32b": 200, "deepseek-coder-v2:16b": 160, "qwen2.5-coder:7b": 120,
    "starcoder2:7b": 100, "codellama:7b": 90,
    # reason
    "deepseek-r1:70b": 200, "qwq:32b": 190, "deepseek-r1:32b": 170,
    "deepseek-r1:14b": 140, "deepseek-r1:7b": 110,
}


def _quality(tag: str) -> int:
    """Return quality score, trying full tag then stem (before ':')."""
    if tag in _QUALITY:
        return _QUALITY[tag]
    # try stem match (e.g. "llama3.3" matches "llama3.3:70b" entry)
    stem = tag.split(":")[0]
    for k, v in _QUALITY.items():
        if k.split(":")[0] == stem:
            return v
    return 50


def fitting_hardware(vram_gb: float | None, ram_gb: float) -> list[ModelInfo]:
    """Return models from CATALOG that fit the available hardware."""
    if vram_gb is not None:
        budget = vram_gb
    else:
        budget = ram_gb * 0.55  # CPU-only: ollama uses ~55% of RAM budget for 4-bit
    return [m for m in CATALOG if m.vram_gb <= budget]


def _norm(tag: str) -> str:
    """Local copy of cli._normalize_tag — drop :latest, lowercase."""
    n = (tag or "").strip().lower()
    if n.endswith(":latest"):
        n = n[: -len(":latest")]
    return n


def _matches_pulled(tag: str, pulled: set[str]) -> bool:
    """A catalog tag matches the pulled set if its stem matches any pulled stem."""
    if not pulled:
        return False
    pulled_norm = {_norm(p) for p in pulled}
    t = _norm(tag)
    if t in pulled_norm:
        return True
    stem = t.split(":")[0]
    return any(p.split(":")[0] == stem for p in pulled_norm)


def best_for_role(
    role: str,
    available_vram: float | None,
    available_ram: float,
    pulled: set[str],
) -> ModelInfo | None:
    """
    Return the highest-quality catalog model for a role that fits hardware,
    preferring already-pulled models to avoid needing a download.
    """
    fits = fitting_hardware(available_vram, available_ram)
    candidates = [m for m in fits if role in m.roles]
    if not candidates:
        return None
    # prefer pulled, then rank by quality descending
    candidates.sort(key=lambda m: (_quality(m.tag), _matches_pulled(m.tag, pulled)), reverse=True)
    return candidates[0]


def _vram_for_tag(tag: str) -> float:
    """Estimate VRAM for an arbitrary tag using catalog stem-match."""
    norm = _norm(tag)
    for m in CATALOG:
        if _norm(m.tag) == norm:
            return m.vram_gb
    stem = norm.split(":")[0]
    for m in CATALOG:
        if _norm(m.tag).split(":")[0] == stem:
            return m.vram_gb
    return 0.0


def recommendations(
    current: dict[str, str],   # role → current model tag (or "")
    pulled:  set[str],
    vram_gb: float | None,
    ram_gb:  float,
) -> list[dict]:
    """
    Return a list of {role, current, suggested, reason, upgrade, downgrade} dicts.
    Surfaces:
      - missing assignments
      - quality upgrades available (within budget)
      - DOWNGRADES needed when current model exceeds available memory
    """
    budget = vram_gb if vram_gb is not None else ram_gb * 0.55
    results = []
    for role in ROLE_KEYS:
        current_tag  = current.get(role, "")
        cur_stem     = current_tag.split(":")[0]
        best         = best_for_role(role, vram_gb, ram_gb, pulled)
        if best is None:
            continue
        cur_quality  = _quality(cur_stem)
        best_quality = _quality(best.tag)
        cur_vram     = _vram_for_tag(current_tag)
        is_missing   = not current_tag
        is_oversize  = bool(current_tag) and cur_vram > budget + 0.5
        is_upgrade   = best_quality > cur_quality + 5

        if is_missing or is_oversize or is_upgrade:
            if is_oversize:
                reason = (f"current {current_tag} needs ~{cur_vram:.1f} GB but you have "
                          f"{budget:.1f} GB — DOWNGRADE recommended")
            elif is_missing:
                reason = "not assigned"
            else:
                reason = f"higher quality ({best.params}, {best.license})"
            results.append({
                "role":      role,
                "current":   current_tag or "—",
                "suggested": best.tag,
                "reason":    reason,
                "upgrade":   is_upgrade and not is_oversize,
                "downgrade": is_oversize,
                "license":   best.license,
                "vram":      best.vram_gb,
                "params":    best.params,
            })
    return results


# ── FOSS tool registry ─────────────────────────────────────────────────────────

class ToolInfo(NamedTuple):
    name:        str          # short identifier
    description: str          # one-line summary
    license:     str          # SPDX
    check_cmd:   str          # shell command to test if installed (e.g. "bat --version")
    install_fn:  str          # name of install function in this module
    theme_fn:    str | None   # name of theme-apply function (or None)
    category:    str          # "cli" | "editor" | "shell" | "git" | "monitor"


TOOL_REGISTRY: list[ToolInfo] = [
    ToolInfo("bat",      "cat with syntax highlighting",     "Apache 2.0",  "bat --version",      "install_bat",      "theme_bat",      "cli"),
    ToolInfo("eza",      "modern ls replacement",            "MIT",         "eza --version",      "install_eza",      "theme_eza",      "cli"),
    ToolInfo("ripgrep",  "extremely fast grep (rg)",         "MIT/Unlicense","rg --version",      "install_ripgrep",  None,             "cli"),
    ToolInfo("fd",       "simple, fast find replacement",    "MIT/Apache 2.0","fd --version",     "install_fd",       None,             "cli"),
    ToolInfo("fzf",      "command-line fuzzy finder",        "MIT",         "fzf --version",      "install_fzf",      "theme_fzf",      "cli"),
    ToolInfo("delta",    "syntax-highlighting git diff pager","Apache 2.0", "delta --version",    "install_delta",    "theme_delta",     "git"),
    ToolInfo("zellij",   "terminal multiplexer (tmux alt)",  "MIT",         "zellij --version",   "install_zellij",   "theme_zellij",   "cli"),
    ToolInfo("starship", "cross-shell prompt",               "ISC",         "starship --version", "install_starship", "theme_starship", "shell"),
    ToolInfo("atuin",    "magical shell history",            "MIT",         "atuin --version",    "install_atuin",    "theme_atuin",    "shell"),
    ToolInfo("bottom",   "graphical process/system monitor", "MIT",         "btm --version",      "install_bottom",   "theme_bottom",   "monitor"),
    ToolInfo("dust",     "more intuitive du",                "Apache 2.0",  "dust --version",     "install_dust",     None,             "cli"),
    ToolInfo("tokei",    "count lines of code",              "MIT/Apache 2.0","tokei --version",  "install_tokei",    None,             "cli"),
    ToolInfo("procs",    "modern ps replacement",            "MIT",         "procs --version",    "install_procs",    None,             "cli"),
    ToolInfo("yazi",     "terminal file manager",            "MIT",         "yazi --version",     "install_yazi",     "theme_yazi",     "cli"),
    ToolInfo("helix",    "post-modern modal editor",         "MPL-2.0",     "hx --version",       "install_helix",    "theme_helix",    "editor"),
    ToolInfo("zoxide",   "smarter cd command",               "MIT",         "zoxide --version",   "install_zoxide",   None,             "shell"),
]

# ── LCARS / Doom Emacs colour palette for theme templates ─────────────────────
# This is what bat/delta/starship/fzf/etc. theme generators emit. It tracks
# whichever mode (dark/light) the user has selected — see org_llm/ui.py for
# DARK_PALETTE and LIGHT_PALETTE. Read fresh on each access via _palette()
# so a runtime `theme` config change is picked up next call.

def _palette() -> dict[str, str]:
    from . import ui as _ui
    p = _ui.PALETTE
    return {
        "orange":  p["lcars1"],
        "purple":  p["lcars2"],
        "blue":    p["lcars3"],
        "green":   p["doom.green"],
        "cyan":    p["doom.cyan"],
        "magenta": p["doom.magenta"],
        "red":     p["doom.red"],
        "yellow":  p["doom.yellow"],
        "bg":      p["bg"],
        "fg":      p["fg"],
        "dim":     p["dim"],
    }


class _PaletteProxy(dict):
    """Dict that re-reads from ui.PALETTE on every access — keeps tool theme
    files in sync with the active mode without forcing every theme_* function
    to take a palette argument.
    """
    def __getitem__(self, key):
        return _palette()[key]

    def __contains__(self, key):
        return key in _palette()

    def __iter__(self):
        return iter(_palette())

    def __len__(self):
        return len(_palette())

    def items(self):
        return _palette().items()

    def keys(self):
        return _palette().keys()

    def values(self):
        return _palette().values()

    def get(self, key, default=None):
        return _palette().get(key, default)


PALETTE = _PaletteProxy()


def _github_latest(owner: str, repo: str) -> str:
    """Return latest release tag (without leading v) from GitHub."""
    import urllib.request, json
    with urllib.request.urlopen(
        f"https://api.github.com/repos/{owner}/{repo}/releases/latest", timeout=8
    ) as r:
        return json.loads(r.read())["tag_name"].lstrip("v")


def _arch_slug() -> str:
    import platform
    m = platform.machine().lower()
    return "x86_64" if m in ("x86_64", "amd64") else "aarch64"


def _install_binary_from_github(
    owner: str, repo: str, asset_pattern: str,
    bin_name: str, bin_dir: str = "~/.local/bin",
) -> bool:
    """Generic: download a tarball/zip from GitHub releases and extract binary."""
    import subprocess, tarfile, zipfile, tempfile, urllib.request
    from pathlib import Path
    dest = Path(bin_dir).expanduser() / bin_name
    try:
        ver = _github_latest(owner, repo)
        url = asset_pattern.format(ver=ver, arch=_arch_slug())
        with tempfile.NamedTemporaryFile(suffix=url.rsplit(".", 1)[-1]) as tmp:
            urllib.request.urlretrieve(url, tmp.name)
            if url.endswith(".tar.gz") or url.endswith(".tgz"):
                with tarfile.open(tmp.name) as tf:
                    for m in tf.getmembers():
                        if m.name.endswith(f"/{bin_name}") or m.name == bin_name:
                            m.name = bin_name
                            tf.extract(m, path=dest.parent)
                            break
            elif url.endswith(".zip"):
                with zipfile.ZipFile(tmp.name) as zf:
                    for entry in zf.namelist():
                        if entry.endswith(f"/{bin_name}") or entry == bin_name:
                            data = zf.read(entry)
                            dest.write_bytes(data)
                            break
            else:
                # raw binary
                import shutil
                shutil.copy(tmp.name, dest)
        dest.chmod(0o755)
        return dest.exists()
    except Exception as exc:
        return False


def install_bat(bin_dir: str = "~/.local/bin") -> bool:
    arch = "x86_64" if _arch_slug() == "x86_64" else "aarch64"
    return _install_binary_from_github(
        "sharkdp", "bat",
        f"https://github.com/sharkdp/bat/releases/download/v{{ver}}/bat-v{{ver}}-{arch}-unknown-linux-musl.tar.gz",
        "bat", bin_dir,
    )


def install_eza(bin_dir: str = "~/.local/bin") -> bool:
    arch = "x86_64" if _arch_slug() == "x86_64" else "aarch64"
    return _install_binary_from_github(
        "eza-community", "eza",
        f"https://github.com/eza-community/eza/releases/download/v{{ver}}/eza_{arch}-unknown-linux-musl.tar.gz",
        "eza", bin_dir,
    )


def install_ripgrep(bin_dir: str = "~/.local/bin") -> bool:
    arch = "x86_64" if _arch_slug() == "x86_64" else "aarch64"
    return _install_binary_from_github(
        "BurntSushi", "ripgrep",
        f"https://github.com/BurntSushi/ripgrep/releases/download/{{ver}}/ripgrep-{{ver}}-{arch}-unknown-linux-musl.tar.gz",
        "rg", bin_dir,
    )


def install_fd(bin_dir: str = "~/.local/bin") -> bool:
    arch = "x86_64" if _arch_slug() == "x86_64" else "aarch64"
    return _install_binary_from_github(
        "sharkdp", "fd",
        f"https://github.com/sharkdp/fd/releases/download/v{{ver}}/fd-v{{ver}}-{arch}-unknown-linux-musl.tar.gz",
        "fd", bin_dir,
    )


def install_delta(bin_dir: str = "~/.local/bin") -> bool:
    arch = "x86_64" if _arch_slug() == "x86_64" else "aarch64"
    return _install_binary_from_github(
        "dandavison", "delta",
        f"https://github.com/dandavison/delta/releases/download/{{ver}}/delta-{{ver}}-{arch}-unknown-linux-musl.tar.gz",
        "delta", bin_dir,
    )


def install_dust(bin_dir: str = "~/.local/bin") -> bool:
    arch = "x86_64" if _arch_slug() == "x86_64" else "aarch64"
    return _install_binary_from_github(
        "bootandy", "dust",
        f"https://github.com/bootandy/dust/releases/download/v{{ver}}/dust-v{{ver}}-{arch}-unknown-linux-musl.tar.gz",
        "dust", bin_dir,
    )


def install_tokei(bin_dir: str = "~/.local/bin") -> bool:
    arch = "x86_64" if _arch_slug() == "x86_64" else "aarch64"
    return _install_binary_from_github(
        "XAMPPRocky", "tokei",
        f"https://github.com/XAMPPRocky/tokei/releases/download/v{{ver}}/tokei-{arch}-unknown-linux-musl.tar.gz",
        "tokei", bin_dir,
    )


def install_procs(bin_dir: str = "~/.local/bin") -> bool:
    arch = "x86_64" if _arch_slug() == "x86_64" else "aarch64"
    return _install_binary_from_github(
        "dalance", "procs",
        f"https://github.com/dalance/procs/releases/download/v{{ver}}/procs-v{{ver}}-{arch}-linux.zip",
        "procs", bin_dir,
    )


def install_fzf(bin_dir: str = "~/.local/bin") -> bool:
    """Install fzf via official install script."""
    import subprocess
    from pathlib import Path
    result = subprocess.run(
        ["curl", "-fsSL", "-o", "/tmp/fzf-install.sh",
         "https://raw.githubusercontent.com/junegunn/fzf/master/install"],
        capture_output=True, timeout=15,
    )
    if result.returncode != 0:
        return False
    Path("/tmp/fzf-install.sh").chmod(0o755)
    r = subprocess.run(
        ["bash", "/tmp/fzf-install.sh", "--bin"],
        capture_output=True, timeout=60,
    )
    # fzf installs to ~/.fzf/bin; symlink to bin_dir
    fzf_bin = Path("~/.fzf/bin/fzf").expanduser()
    if fzf_bin.exists():
        dest = Path(bin_dir).expanduser() / "fzf"
        if not dest.exists():
            dest.symlink_to(fzf_bin)
        return True
    return r.returncode == 0


def install_zoxide(bin_dir: str = "~/.local/bin") -> bool:
    """Install zoxide via official install script."""
    import subprocess
    r = subprocess.run(
        ["bash", "-c",
         f"curl -sS https://raw.githubusercontent.com/ajeetdsouza/zoxide/main/install.sh | bash -s -- --bin-dir {bin_dir}"],
        capture_output=True, timeout=60,
    )
    return r.returncode == 0


def install_starship(bin_dir: str = "~/.local/bin") -> bool:
    """Install starship via official install script."""
    import subprocess
    r = subprocess.run(
        ["bash", "-c",
         f"curl -sS https://starship.rs/install.sh | bash -s -- --yes --bin-dir {bin_dir}"],
        capture_output=True, timeout=60,
    )
    return r.returncode == 0


def install_atuin(bin_dir: str = "~/.local/bin") -> bool:
    """Install atuin via official install script."""
    import subprocess
    result = subprocess.run(
        ["bash", "-c",
         "curl --proto '=https' --tlsv1.2 -LsSf https://setup.atuin.sh | bash"],
        capture_output=True, timeout=60,
    )
    return result.returncode == 0


def install_bottom(bin_dir: str = "~/.local/bin") -> bool:
    arch = "x86_64" if _arch_slug() == "x86_64" else "aarch64"
    return _install_binary_from_github(
        "ClementTsang", "bottom",
        f"https://github.com/ClementTsang/bottom/releases/download/{{ver}}/bottom_{arch}-unknown-linux-musl.tar.gz",
        "btm", bin_dir,
    )


def install_zellij(bin_dir: str = "~/.local/bin") -> bool:
    arch = "x86_64" if _arch_slug() == "x86_64" else "aarch64"
    return _install_binary_from_github(
        "zellij-org", "zellij",
        f"https://github.com/zellij-org/zellij/releases/download/v{{ver}}/zellij-{arch}-unknown-linux-musl.tar.gz",
        "zellij", bin_dir,
    )


def install_helix(bin_dir: str = "~/.local/bin") -> bool:
    arch = "x86_64" if _arch_slug() == "x86_64" else "aarch64"
    return _install_binary_from_github(
        "helix-editor", "helix",
        f"https://github.com/helix-editor/helix/releases/download/{{ver}}/helix-{{ver}}-{arch}-linux.tar.xz",
        "hx", bin_dir,
    )


def install_yazi(bin_dir: str = "~/.local/bin") -> bool:
    arch = "x86_64" if _arch_slug() == "x86_64" else "aarch64"
    return _install_binary_from_github(
        "sxyazi", "yazi",
        f"https://github.com/sxyazi/yazi/releases/download/v{{ver}}/yazi-{arch}-unknown-linux-musl.tar.gz",
        "yazi", bin_dir,
    )


# ── Theme templates ────────────────────────────────────────────────────────────

def theme_bat() -> str:
    """Return bat config snippet using Doom/LCARS palette."""
    return f"""\
# bat config — org-llm themed
# ~/.config/bat/config
--theme="TwoDark"
--style="numbers,changes,header"
--color=always
"""


def theme_delta() -> str:
    """Return delta git config snippet with LCARS palette."""
    return f"""\
# Add to ~/.gitconfig  [delta] section
[delta]
    navigate = true
    side-by-side = true
    line-numbers = true
    syntax-theme = "TwoDark"
    plus-style = "syntax #{PALETTE['green'].lstrip('#')}"
    minus-style = "syntax #{PALETTE['red'].lstrip('#')}"
    line-numbers-plus-style = "{PALETTE['green']}"
    line-numbers-minus-style = "{PALETTE['red']}"
    line-numbers-left-style = "{PALETTE['dim']}"
    line-numbers-right-style = "{PALETTE['dim']}"
    file-style = "bold {PALETTE['orange']}"
    hunk-header-style = "file line-number syntax"
    hunk-header-decoration-style = "{PALETTE['purple']} box"

[core]
    pager = delta
"""


def theme_starship() -> str:
    """Return starship.toml with LCARS/Doom theming."""
    # Starship uses """...""" in TOML for multi-line format strings;
    # build with concatenation to avoid triple-quote collision in the f-string.
    fmt = (
        f'[┌─](bold {PALETTE["orange"]})'
        f'[$username](bold {PALETTE["purple"]})'
        f'[@](bold {PALETTE["dim"]})'
        f'[$hostname](bold {PALETTE["blue"]})'
        f' in [$directory](bold {PALETTE["cyan"]})'
        '$git_branch$git_status\n'
        f'[└─▶](bold {PALETTE["orange"]}) '
    )
    return (
        f'# starship.toml — org-llm LCARS theme\n'
        f'format = """\n{fmt}"""\n\n'
        f'[username]\n'
        f'style_user = "bold {PALETTE["purple"]}"\n'
        f'style_root = "bold {PALETTE["red"]}"\n'
        f'show_always = false\n\n'
        f'[directory]\n'
        f'style = "bold {PALETTE["cyan"]}"\n'
        f'truncation_length = 3\n'
        f'truncate_to_repo = true\n\n'
        f'[git_branch]\n'
        f'format = " on [$symbol$branch](bold {PALETTE["magenta"]})"\n'
        f'symbol = " "\n\n'
        f'[git_status]\n'
        f'format = \'([$all_status$ahead_behind]({PALETTE["yellow"]}) )\'\n\n'
        f'[python]\n'
        f'format = " via [$symbol$version]({PALETTE["green"]}) "\n'
        f'symbol = " "\n\n'
        f'[rust]\n'
        f'format = " via [$symbol$version]({PALETTE["orange"]}) "\n'
    )


def theme_fzf() -> str:
    """Return fzf env config with LCARS palette."""
    return f"""\
# Add to ~/.profile or ~/.bashrc / ~/.config/fish/config.fish
export FZF_DEFAULT_OPTS='
  --color=bg+:{PALETTE["bg"]},bg:{PALETTE["bg"]},spinner:{PALETTE["cyan"]},hl:{PALETTE["orange"]}
  --color=fg:{PALETTE["fg"]},header:{PALETTE["orange"]},info:{PALETTE["purple"]},pointer:{PALETTE["cyan"]}
  --color=marker:{PALETTE["green"]},fg+:{PALETTE["fg"]},prompt:{PALETTE["orange"]},hl+:{PALETTE["cyan"]}
  --border rounded
  --height 40%
'
"""


def theme_zellij() -> str:
    """Return zellij config KDL snippet with LCARS palette."""
    return f"""\
// zellij config  (~/.config/zellij/config.kdl)
themes {{
    lcars {{
        fg "{PALETTE['fg']}"
        bg "{PALETTE['bg']}"
        black "{PALETTE['bg']}"
        red "{PALETTE['red']}"
        green "{PALETTE['green']}"
        yellow "{PALETTE['yellow']}"
        blue "{PALETTE['blue']}"
        magenta "{PALETTE['magenta']}"
        cyan "{PALETTE['cyan']}"
        white "{PALETTE['fg']}"
        orange "{PALETTE['orange']}"
    }}
}}
theme "lcars"
"""


def theme_bottom() -> str:
    """Return bottom config with LCARS palette."""
    return f"""\
# bottom config (~/.config/bottom/bottom.toml)
[colors]
table_header_color = "{PALETTE['orange']}"
all_cpu_color = "{PALETTE['cyan']}"
avg_cpu_color = "{PALETTE['purple']}"
cpu_core_colors = ["{PALETTE['blue']}", "{PALETTE['green']}", "{PALETTE['cyan']}", "{PALETTE['magenta']}"]
ram_color = "{PALETTE['green']}"
swap_color = "{PALETTE['yellow']}"
rx_color = "{PALETTE['cyan']}"
tx_color = "{PALETTE['orange']}"
widget_title_color = "{PALETTE['purple']}"
border_color = "{PALETTE['dim']}"
highlighted_border_color = "{PALETTE['orange']}"
text_color = "{PALETTE['fg']}"
selected_text_color = "{PALETTE['bg']}"
selected_bg_color = "{PALETTE['orange']}"
"""


def theme_atuin() -> str:
    """Return atuin config snippet."""
    return """\
# ~/.config/atuin/config.toml
style = "compact"
show_preview = true
max_preview_height = 4
"""


def theme_eza() -> str:
    """Return eza shell alias block."""
    return """\
# Add to ~/.bashrc / ~/.config/fish/config.fish
# eza aliases (replace ls)
alias ls='eza --icons --group-directories-first'
alias ll='eza --icons --long --group-directories-first'
alias la='eza --icons --long --all --group-directories-first'
alias lt='eza --icons --tree --level=2 --group-directories-first'
"""


def theme_helix() -> str:
    """Return helix config.toml with Doom dark theme."""
    return """\
# ~/.config/helix/config.toml
theme = "dark_plus"

[editor]
line-number = "relative"
mouse = true
auto-pairs = true
auto-save = true
color-modes = true
indent-guides.render = true

[editor.cursor-shape]
insert = "bar"
normal = "block"
select = "underline"

[editor.statusline]
left = ["mode", "spinner", "file-name"]
right = ["diagnostics", "selections", "position", "file-encoding", "file-type"]
"""


def theme_yazi() -> str:
    """Return yazi flavour config pointing to a dark theme."""
    return """\
# ~/.config/yazi/theme.toml  (yazi --clear-cache after applying)
[flavor]
use = "catppuccin-mocha"
"""


def apply_theme(tool_name: str, config_dir: str = "~/.config") -> tuple[bool, str]:
    """
    Apply theme for tool_name. Returns (success, config_path_written).
    Writes config snippet to the appropriate dotfile path.
    """
    from pathlib import Path
    config_home = Path(config_dir).expanduser()

    theme_map: dict[str, tuple[str, str]] = {
        # tool_name: (theme_fn_name, target_path relative to config_home)
        "bat":      ("theme_bat",      "bat/config"),
        "delta":    ("theme_delta",    "git/delta_config.snippet"),
        "starship": ("theme_starship", "starship.toml"),
        "fzf":      ("theme_fzf",      "fzf/fzf.env"),
        "zellij":   ("theme_zellij",   "zellij/config.kdl"),
        "bottom":   ("theme_bottom",   "bottom/bottom.toml"),
        "atuin":    ("theme_atuin",    "atuin/config.toml"),
        "eza":      ("theme_eza",      "eza/aliases.sh"),
        "helix":    ("theme_helix",    "helix/config.toml"),
        "yazi":     ("theme_yazi",     "yazi/theme.toml"),
    }
    if tool_name not in theme_map:
        return False, ""

    fn_name, rel_path = theme_map[tool_name]
    import sys
    fn = getattr(sys.modules[__name__], fn_name, None)
    if not fn:
        return False, ""

    target = config_home / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    content = fn()
    if not target.exists():
        target.write_text(content)
        return True, str(target)
    return False, str(target)  # already exists — don't clobber


def get_tool(name: str) -> ToolInfo | None:
    return next((t for t in TOOL_REGISTRY if t.name == name), None)
# models.py:1 ends here
