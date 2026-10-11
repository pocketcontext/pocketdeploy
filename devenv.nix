{ pkgs, ... }:

{
  packages = with pkgs; [ python312 uv oci-cli google-cloud-sdk openssh git gh curl jq sqlite ];
  languages.javascript = {
    enable = true;
    package = pkgs.nodejs_22;
    npm = {
      enable = true;
      install.enable = true;
    };
  };
  enterShell = ''
    export PATH="$DEVENV_ROOT/node_modules/.bin:$PATH"
  '';
  scripts.vaultcontext.exec = ''
    uv tool run --from 'vaultcontext-client @ git+https://github.com/pocketcontext/vaultcontext.git@5157597a4ea9f33cb9806b1485bb84f01b5cf276' vaultcontext "$@"
  '';
  env.UV_PYTHON = "${pkgs.python312}/bin/python3";
  scripts.pocketdeploy.exec = ''
    uv run --project "$DEVENV_ROOT" pocketdeploy "$@"
  '';
}
