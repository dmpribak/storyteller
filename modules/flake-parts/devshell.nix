{ ... }:
{
  perSystem =
    { pkgs, ... }:
    {
      devshells.default = {
        packages = [
          pkgs.python3
          pkgs.uv
        ];

        env = [
          # Use the Nix interpreter; uv's downloaded Pythons don't run on NixOS.
          { name = "UV_PYTHON"; value = "${pkgs.python3}/bin/python3"; }
          { name = "UV_PYTHON_DOWNLOADS"; value = "never"; }
        ];

        devshell.startup.storyteller.text = ''
          echo "❄️devshell started (uv sync, then uv run bot.py)"
        '';
      };
    };
}
