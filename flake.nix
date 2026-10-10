{
  description = "Pinned CLI tools for PocketDeploy deployment shells";

  inputs.nixpkgs.url = "github:cachix/devenv-nixpkgs/rolling";

  outputs = { nixpkgs, ... }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "aarch64-darwin" ];
    in {
      packages = nixpkgs.lib.genAttrs systems (system:
        let pkgs = import nixpkgs { inherit system; };
        in { cloud-clis = import ./nix/cloud-clis.nix { inherit pkgs; }; });
    };
}
