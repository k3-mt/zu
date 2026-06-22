# custom-tool — test fixture (not a shipped example)

A bundle fixture used by the test suite to exercise **custom tools + bundle/pack**: a `tools/`
package referenced as `tools.greet:Greet` in the tier ladder and loaded by running the
directory — no packaging, no `pip install`, no entry point. Backs `test_example_agents.py`
(the bundle-runs-via-CLI test) and the docker `zu pack` test in `test_sandbox.py`.

```sh
zu run  packages/zu-cli/tests/agents/custom-tool/                 # offline, no key
zu pack packages/zu-cli/tests/agents/custom-tool/ -t my-agent:1   # bake into a standalone image
```

The pattern — drop a `module:Class` in `tools/`, name it in `tiers:` (the registration), pick
its tier on that same line — is how any bundle ships its own tools. The sole shipped example
agent is `examples/agents/vet-appointment/`.
