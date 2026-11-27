"""A later resume inherits approved limits and the immutable runtime identity."""
import copy
from pathlib import Path

from bench.prompt_repo_eval import resume_config

manifest = {
    'arguments': {'token_cap': 10_000_000, 'episode_token_cap': 1_000_000,
                  'call_cap': 10000, 'image': 'expired-label', 'model': 'frozen-model'},
    'image': 'sha256:fixed-runtime',
    'budget_history': [
        {'token_cap': 10_000_000, 'episode_token_cap': 1_000_000, 'call_cap': 10000},
        {'token_cap': 24_000_000, 'episode_token_cap': 3_000_000, 'call_cap': 20000},
    ],
}
original = copy.deepcopy(manifest)
config = resume_config(manifest, Path('/tmp/saved-run'))
assert (config.token_cap, config.episode_token_cap, config.call_cap) == (24_000_000, 3_000_000, 20000)
assert config.image == 'sha256:fixed-runtime' and config.model == 'frozen-model'
assert config.resume == '/tmp/saved-run'
config = resume_config(manifest, Path('/tmp/saved-run'), token_cap=30_000_000)
assert (config.token_cap, config.episode_token_cap) == (30_000_000, 3_000_000)
config = resume_config(manifest, Path('/tmp/saved-run'), episode_token_cap=4_000_000)
assert (config.token_cap, config.episode_token_cap) == (24_000_000, 4_000_000)
assert manifest == original
manifest.pop('budget_history')
config = resume_config(manifest, Path('/tmp/old-run'))
assert (config.token_cap, config.episode_token_cap) == (10_000_000, 1_000_000)
print('REPO RESUME CONFIG OK — inherited/explicit caps, immutable image, legacy fallback; no model calls')
