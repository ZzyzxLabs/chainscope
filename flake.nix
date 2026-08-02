{
  description = "chainscope --- blockchain forensics with provenance in the type system";

  # Pinned in flake.lock, which is committed. That is the whole reason to offer
  # a flake at all: `nix develop` two years from now builds the same thing,
  # including the transitive C libraries that a requirements file does not
  # pin and that break a reproduction long before any Python package does.
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };
        python = pkgs.python312;

        # Runtime dependencies only. The optional extras are listed separately
        # so `nix run` stays small and `nix develop` gets everything.
        core = ps: with ps; [ httpx platformdirs ];

        extras = ps: with ps; [
          duckdb
          eth-utils
          eth-abi
          # eth-utils delegates keccak to eth-hash, which ships no backend of
          # its own. Declaring eth-utils alone installs cleanly and then fails
          # at the first hash --- pyproject.toml pins the backend for the same
          # reason, and the flake did not carry that across. The Nix job caught
          # it, which is what the job is for.
          eth-hash
          pycryptodome
          base58
          rich
        ];

        dev = ps: with ps; [ pytest pytest-asyncio hypothesis mypy ruff ];

        chainscope = python.pkgs.buildPythonApplication {
          pname = "chainscope";
          version = "0.2.0";
          pyproject = true;
          src = ./.;

          build-system = [ python.pkgs.hatchling ];
          dependencies = core python.pkgs ++ extras python.pkgs;

          # The suite blocks outbound sockets at the fixture level, so this
          # proves the build rather than proving the network was up. The
          # loopback tests are excluded: the Nix sandbox has no network
          # namespace to bind in, and skipping them here is honest --- CI runs
          # them on a machine that does.
          nativeCheckInputs = dev python.pkgs;
          checkPhase = ''
            runHook preCheck
            ${python.pkgs.pytest}/bin/pytest -q --ignore=tests/unit/test_local_server.py
            runHook postCheck
          '';

          meta = with pkgs.lib; {
            description = "Blockchain forensics with provenance in the type system";
            homepage = "https://github.com/ZzyzxLabs/chainscope";
            license = licenses.mit;
            mainProgram = "chainscope";
          };
        };
      in
      {
        packages = {
          default = chainscope;
          inherit chainscope;
        };

        apps = {
          default = flake-utils.lib.mkApp { drv = chainscope; };
          # `nix run .#mcp` --- the agent surface, without installing anything.
          mcp = flake-utils.lib.mkApp {
            drv = chainscope;
            name = "chainscope-mcp";
          };
          serve = flake-utils.lib.mkApp {
            drv = chainscope;
            name = "chainscope-serve";
          };
        };

        devShells.default = pkgs.mkShell {
          packages = [
            (python.withPackages (ps: core ps ++ extras ps ++ dev ps))
            pkgs.git
            pkgs.graphviz # renders the DOT export without a browser
            pkgs.uv
          ];

          shellHook = ''
            echo "chainscope dev shell --- $(python --version)"
            echo "  pytest -q          the suite, offline"
            echo "  scripts/ci-local.sh  the same gates CI runs"
            export PYTHONPATH="$PWD/src:$PYTHONPATH"
          '';
        };
      });
}
