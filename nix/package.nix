{ pkgs }:

let
  python = pkgs.python3.withPackages (ps: [ ps.requests ]);
in
pkgs.stdenvNoCC.mkDerivation {
  pname = "github-forgejo-mirror";
  version = "1.0.0";
  src = ../.;
  dontBuild = true;
  nativeBuildInputs = [ pkgs.makeWrapper ];

  installPhase = ''
    install -Dm644 $src/github_forgejo_mirror.py $out/lib/github-forgejo-mirror/github_forgejo_mirror.py
    makeWrapper ${python}/bin/python $out/bin/github-forgejo-mirror \
      --add-flags $out/lib/github-forgejo-mirror/github_forgejo_mirror.py
  '';
}
