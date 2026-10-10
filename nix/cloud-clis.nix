{ pkgs }:
pkgs.buildNpmPackage {
    pname = "pocketdeploy-cloud-clis";
    version = "1";
    src = ../tools/cloud-clis;
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
  }
