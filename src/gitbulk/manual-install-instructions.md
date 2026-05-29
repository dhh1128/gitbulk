gitbulk manual install
=======================

The one-line installer could not finish on its own. Here is how to install
gitbulk by hand.

1. Download the release binary (requires the GitHub CLI, authenticated):

       gh release download --repo dhh1128/gitbulk --pattern gitbulk --dir /tmp

   If you cannot use `gh`, download the asset named `gitbulk` from the
   latest release page in a browser:

       https://github.com/dhh1128/gitbulk/releases/latest

2. Make it executable and move it onto your PATH:

       chmod +x /tmp/gitbulk
       mkdir -p ~/.local/bin
       mv /tmp/gitbulk ~/.local/bin/gitbulk

3. Ensure ~/.local/bin is on your PATH. Add ONE of these to your shell
   profile if it is not already there:

       # bash (~/.bashrc) or zsh (~/.zshrc)
       export PATH="$HOME/.local/bin:$PATH"

       # fish
       fish_add_path ~/.local/bin

4. Verify:

       gitbulk --version

gitbulk also requires the GitHub CLI (`gh`) and `git`, authenticated for
your account. See https://github.com/dhh1128/gitbulk for full setup.
