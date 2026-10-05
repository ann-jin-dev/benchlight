#!/usr/bin/env bash
set -euo pipefail

# Ubuntu 24.04 host setup for Docker Engine, Compose, and NVIDIA Container Toolkit.
# Uses the official signed APT repositories; safe to re-run for package updates.

# shellcheck source=/dev/null
if [[ "$(. /etc/os-release; printf '%s' "$ID")" != "ubuntu" ]]; then
  echo "This setup script expects Ubuntu." >&2
  exit 1
fi

sudo -n true
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg

temporary_dir="$(mktemp -d)"
trap 'rm -rf "$temporary_dir"' EXIT

curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o "$temporary_dir/docker.asc"
sudo install -m 0755 -d /etc/apt/keyrings
sudo install -m 0644 "$temporary_dir/docker.asc" /etc/apt/keyrings/docker.asc

# shellcheck source=/dev/null
ubuntu_codename="$(. /etc/os-release; printf '%s' "${UBUNTU_CODENAME:-$VERSION_CODENAME}")"
architecture="$(dpkg --print-architecture)"
cat > "$temporary_dir/docker.sources" <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $ubuntu_codename
Components: stable
Architectures: $architecture
Signed-By: /etc/apt/keyrings/docker.asc
EOF
sudo install -m 0644 "$temporary_dir/docker.sources" /etc/apt/sources.list.d/docker.sources

curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey -o "$temporary_dir/nvidia.gpg.asc"
gpg --dearmor < "$temporary_dir/nvidia.gpg.asc" > "$temporary_dir/nvidia.gpg"
sudo install -m 0644 "$temporary_dir/nvidia.gpg" /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  > "$temporary_dir/nvidia-container-toolkit.list"
sudo install -m 0644 "$temporary_dir/nvidia-container-toolkit.list" \
  /etc/apt/sources.list.d/nvidia-container-toolkit.list

sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl enable --now docker
sudo systemctl restart docker

if ! id -nG "$USER" | tr ' ' '\n' | grep -qx docker; then
  sudo usermod -aG docker "$USER"
  echo "Added $USER to the docker group. Sign out and back in to use Docker without sudo."
fi

docker --version
docker compose version
nvidia-ctk --version
