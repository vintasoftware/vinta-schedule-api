#!/bin/bash

# Add bashrc additions
cat /home/user/app/.devcontainer/config-files/bashrc.additions >> ~/.bashrc

# oh-my-zsh & plugins (check if oh-my-zsh is already installed)
if [ -d "$HOME/.oh-my-zsh" ]; then
    curl -s https://ohmyposh.dev/install.sh | bash -s

    # Clone plugins
    git clone https://github.com/zsh-users/zsh-autosuggestions ${ZSH_CUSTOM:-~/.oh-my-zsh/custom}/plugins/zsh-autosuggestions
    git clone https://github.com/zsh-users/zsh-syntax-highlighting.git ${ZSH_CUSTOM:-~/.oh-my-zsh/custom}/plugins/zsh-syntax-highlighting

    # Copy config files to home directory
    cp /home/user/app/.devcontainer/config-files/.zshrc ~/

    # Enable plugins
    sed -i 's/^plugins=.*/plugins=(git zsh-autosuggestions zsh-syntax-highlighting)/' ~/.zshrc

    oh-my-posh init zsh --config "pure"
    oh-my-posh font install firacode

    # initialize oh-my-posh theme by appending eval "$(oh-my-posh init zsh --config "pure")" at the
    # end of .zshrc in a new line, if not already present
    grep -qxF 'eval "$(oh-my-posh init zsh --config "pure")"' ~/.zshrc || echo -e '\n\neval "$(oh-my-posh init zsh --config "pure"  )"' >> ~/.zshrc

    zsh -n ~/.zshrc
fi
