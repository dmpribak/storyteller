{ self, ... }:
{
  flake.nixosModules.default = self.nixosModules.storyteller;

  flake.nixosModules.storyteller =
    {
      config,
      lib,
      pkgs,
      ...
    }:
    let
      cfg = config.os.services.storyteller;
    in
    {
      options.os.services.storyteller = {
        enabled = lib.mkEnableOption "the storyteller Discord bot";

        package = lib.mkOption {
          type = lib.types.package;
          default = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
          defaultText = lib.literalExpression "storyteller.packages.\${system}.default";
          description = "The storyteller package to run.";
        };

        environmentFile = lib.mkOption {
          type = lib.types.str;
          example = "/run/secrets/storyteller.env";
          description = ''
            Path to a file with `DISCORD_TOKEN=...` (and optionally `DEV_GUILD_ID=...`).
            Kept as a string so the token never lands in the Nix store.
          '';
        };
      };

      config = lib.mkIf cfg.enabled {
        systemd.services.storyteller = {
          description = "Storyteller Discord bot";
          wantedBy = [ "multi-user.target" ];
          wants = [ "network-online.target" ];
          after = [ "network-online.target" ];

          # One JSON save per server lives in /var/lib/storyteller.
          environment.STORYBOT_DATA = "/var/lib/storyteller";

          serviceConfig = {
            ExecStart = lib.getExe' cfg.package "storyteller";
            EnvironmentFile = cfg.environmentFile;
            DynamicUser = true;
            StateDirectory = "storyteller";
            WorkingDirectory = "/var/lib/storyteller";
            Restart = "on-failure";
            RestartSec = "10s";

            NoNewPrivileges = true;
            PrivateTmp = true;
            PrivateDevices = true;
            ProtectSystem = "strict";
            ProtectHome = true;
            ProtectKernelTunables = true;
            ProtectKernelModules = true;
            ProtectControlGroups = true;
            RestrictAddressFamilies = [
              "AF_UNIX"
              "AF_INET"
              "AF_INET6"
            ];
            RestrictNamespaces = true;
            LockPersonality = true;
            MemoryDenyWriteExecute = true;
            SystemCallArchitectures = "native";
          };
        };
      };
    };
}
