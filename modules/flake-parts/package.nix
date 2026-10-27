# Builds the bot from uv.lock with uv2nix, so Nix and `uv sync` resolve the same versions.
{ inputs, lib, ... }:
let
  workspace = inputs.uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ../..; };
  overlay = workspace.mkPyprojectOverlay { sourcePreference = "wheel"; };
in
{
  perSystem =
    { pkgs, ... }:
    let
      pythonSet =
        (pkgs.callPackage inputs.pyproject-nix.build.packages { python = pkgs.python3; }).overrideScope
          (
            lib.composeManyExtensions [
              inputs.pyproject-build-systems.overlays.wheel
              overlay
            ]
          );
      storyteller = pythonSet.mkVirtualEnv "storyteller-env" workspace.deps.default;
    in
    {
      packages = {
        inherit storyteller;
        default = storyteller;
      };

      apps.default = {
        type = "app";
        program = "${storyteller}/bin/storyteller";
      };
    };
}
