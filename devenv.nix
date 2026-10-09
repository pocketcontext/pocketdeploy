{ pkgs, ... }:
let
  sNail = pkgs.stdenv.mkDerivation {
    pname = "s-nail";
    version = "14.9.25";
    src = pkgs.fetchurl {
      url = "https://ftp.sdaoden.eu/s-nail-14.9.25.tar.xz";
      sha256 = "20ff055be9829b69d46ebc400dfe516a40d287d7ce810c74355d6bdc1a28d8a9";
    };
    buildInputs = [ pkgs.openssl ];
    nativeBuildInputs = [ pkgs.pkg-config ];
    makeFlags = [ "VAL_PREFIX=${placeholder "out"}" "OPT_DOTLOCK=no" "OPT_SMTP=require" "OPT_TLS=require" ];
    preBuild = "patchShebangs .";
    buildFlags = [ "all" ];
  };
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
  packages = with pkgs; [ python312 uv oci-cli openssh git gh curl jq sqlite sNail cloudClis ];
  scripts.vaultcontext.exec = ''
    uv tool run --from 'vaultcontext-client @ git+https://github.com/pocketcontext/vaultcontext.git@5157597a4ea9f33cb9806b1485bb84f01b5cf276' vaultcontext "$@"
  '';
  env.UV_PYTHON = "${pkgs.python312}/bin/python3";
  scripts.pocketdeploy.exec = ''
    uv run --project "$DEVENV_ROOT" pocketdeploy "$@"
  '';
}
