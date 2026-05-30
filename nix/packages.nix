# nix/packages.nix — ATLAZ packages built with uv2nix
{ inputs, ... }:
{
  perSystem =
    { pkgs, lib, inputs', ... }:
    let
      atlazPkg = pkgs.callPackage ./atlaz.nix {
        inherit (inputs) uv2nix pyproject-nix pyproject-build-systems;
        npm-lockfile-fix = inputs'.npm-lockfile-fix.packages.default;
        rev = inputs.self.rev or null;
      };
    in
    {
      packages = {
        default = atlazPkg;

        messaging = atlazPkg.override {
          extraDependencyGroups = [ "messaging" ];
        };

        full = atlazPkg.override {
          extraDependencyGroups = [
            "anthropic"
            "azure-identity"
            "bedrock"
            "daytona"
            "dingtalk"
            "edge-tts"
            "exa"
            "fal"
            "feishu"
            "firecrawl"
            "hindsight"
            "honcho"
            "messaging"
            "modal"
            "parallel-web"
            "tts-premium"
            "voice"
          ] ++ lib.optionals pkgs.stdenv.isLinux [ "matrix" ];
        };

        tui = atlazPkg.atlazTui;
        web = atlazPkg.atlazWeb;

        fix-lockfiles = atlazPkg.atlazNpmLib.mkFixLockfiles {
          packages = [ atlazPkg.atlazTui atlazPkg.atlazWeb ];
        };
      };
    };
}
