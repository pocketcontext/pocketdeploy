{ pkgs, ... }:
let
  cloudClis = pkgs.buildNpmPackage {
    pname = "pocketdeploy-cloud-clis";
    version = "1";
    src = ./tools/cloud-clis;
    nodejs = pkgs.nodejs_22;
    npmDepsHash = "sha256-CJ9JcJVXkbdzKLZ2CMWlY34TwykORh29qr6UulwNTog=";
    dontNpmBuild = true;
    npmFlags = [ "--ignore-scripts" ];
    installPhase = ''
      mkdir -p $out/lib/cloud-clis $out/bin
      cp -r node_modules $out/lib/cloud-clis/
      makeWrapper ${pkgs.nodejs_22}/bin/node $out/bin/cf \
        --add-flags "$out/lib/cloud-clis/node_modules/cf/bin/cf"
      makeWrapper ${pkgs.nodejs_22}/bin/node $out/bin/resend \
        --add-flags "$out/lib/cloud-clis/node_modules/resend-cli/dist/cli.cjs"
    '';
  };
in
{
  packages = with pkgs; [ python312 uv oci-cli google-cloud-sdk openssh git gh curl jq sqlite cloudClis ];
  scripts.vaultcontext.exec = ''
    uv tool run --from 'vaultcontext-client @ git+https://github.com/pocketcontext/vaultcontext.git@5157597a4ea9f33cb9806b1485bb84f01b5cf276' vaultcontext "$@"
  '';
  env.UV_PYTHON = "${pkgs.python312}/bin/python3";
  scripts.pocketdeploy.exec = ''
    uv run --project "$DEVENV_ROOT" pocketdeploy "$@"
  '';
}
