{
  description = "Discover GitHub repositories and provision Forgejo pull mirrors";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
    in
    {
      packages = forAllSystems (system: {
        default = import ./nix/package.nix {
          pkgs = import nixpkgs { inherit system; };
        };
      });

      nixosModules.default = import ./nix/module.nix;
    };
}
