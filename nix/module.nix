{ config, lib, pkgs, ... }:

let
  cfg = config.services.github-forgejo-mirror;
  python = pkgs.python3.withPackages (ps: [ ps.requests ]);
  app = pkgs.stdenvNoCC.mkDerivation {
    pname = "github-forgejo-mirror";
    version = "1.0.0";
    src = ../.;
    dontBuild = true;
    installPhase = ''
      install -Dm644 $src/github_forgejo_mirror.py $out/lib/github-forgejo-mirror/github_forgejo_mirror.py
      makeWrapper ${python}/bin/python $out/bin/github-forgejo-mirror \
        --add-flags $out/lib/github-forgejo-mirror/github_forgejo_mirror.py
    '';
    nativeBuildInputs = [ pkgs.makeWrapper ];
  };
in
{
  options.services.github-forgejo-mirror = {
    enable = lib.mkEnableOption "GitHub to Forgejo personal repository mirroring";
    environmentFile = lib.mkOption {
      # Keep this as a string: secret files must stay outside the Nix store.
      type = lib.types.str;
      default = "/run/secrets/github-forgejo-mirror.env";
      description = "Root-readable systemd EnvironmentFile containing required credentials.";
    };
    package = lib.mkOption {
      type = lib.types.package;
      default = app;
      defaultText = lib.literalExpression "package built from this module's source";
      description = "The GitHub to Forgejo mirror application.";
    };
    interval = lib.mkOption {
      type = lib.types.str;
      default = "15m";
      description = "How often systemd runs the discovery job.";
    };
  };

  config = lib.mkIf cfg.enable {
    systemd.services.github-forgejo-mirror = {
      description = "Discover GitHub repositories and provision Forgejo pull mirrors";
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      serviceConfig = {
        Type = "oneshot";
        User = "github-forgejo-mirror";
        Group = "github-forgejo-mirror";
        DynamicUser = true;
        StateDirectory = "github-forgejo-mirror";
        StateDirectoryMode = "0700";
        EnvironmentFile = cfg.environmentFile;
        Environment = [ "STATE_DIR=/var/lib/github-forgejo-mirror" ];
        ExecStart = "${cfg.package}/bin/github-forgejo-mirror";
        UMask = "0077";
        NoNewPrivileges = true;
        PrivateTmp = true;
        ProtectSystem = "strict";
        ProtectHome = true;
      };
    };

    systemd.timers.github-forgejo-mirror = {
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnBootSec = "5m";
        OnUnitActiveSec = cfg.interval;
        Persistent = true;
      };
    };
  };
}
