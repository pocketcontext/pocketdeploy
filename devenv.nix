{ pkgs, ... }:
{
  packages = with pkgs; [ python312 uv oci-cli openssh git gh curl jq sqlite ];
  env.UV_PYTHON = "${pkgs.python312}/bin/python3";
  scripts.pocketdeploy.exec = ''
    uv run --project "$DEVENV_ROOT" pocketdeploy "$@"
  '';
}
