# =============================================================================
# Cloud-init.
#
# Installs Docker Engine and the Compose plugin from Docker's official
# repository, ensures the SSM Agent is running, mounts the encrypted data
# volume by filesystem UUID (never by device name, which can be renumbered
# across reboots), creates /srv/techsara-chat-archive with restrictive
# permissions and installs the systemd unit.
#
# No secret value appears here: user-data is readable from instance metadata by
# anything on the box, so secrets are fetched from SSM Parameter Store at
# deploy time by scripts/deploy_ec2.sh instead.
# =============================================================================

locals {
  data_root = "/srv/${var.project_name}"

  user_data = <<-CLOUDINIT
    #cloud-config
    package_update: true
    package_upgrade: true

    packages:
      - ca-certificates
      - curl
      - gnupg
      - jq
      - unzip
      - xfsprogs
      - unattended-upgrades

    write_files:
      - path: /etc/apt/apt.conf.d/20auto-upgrades
        content: |
          APT::Periodic::Update-Package-Lists "1";
          APT::Periodic::Unattended-Upgrade "1";

      - path: /usr/local/sbin/mount-data-volume.sh
        permissions: "0700"
        content: |
          #!/bin/bash
          # Mount the attached EBS data volume by UUID, formatting it the first
          # time. Idempotent: safe to re-run on every boot.
          set -euo pipefail
          DATA_ROOT="${local.data_root}"
          mkdir -p "$DATA_ROOT"

          # NVMe renames devices, so find the volume by size and empty mount.
          DEVICE=""
          for candidate in /dev/nvme1n1 /dev/nvme2n1 /dev/xvdf /dev/sdf; do
            if [ -b "$candidate" ] && ! findmnt --source "$candidate" >/dev/null 2>&1; then
              if [ "$(lsblk -no MOUNTPOINT "$candidate" | tr -d ' \n')" = "" ]; then
                DEVICE="$candidate"
                break
              fi
            fi
          done

          if [ -z "$DEVICE" ]; then
            echo "data volume not found yet; will retry on next boot" >&2
            exit 0
          fi

          if ! blkid "$DEVICE" >/dev/null 2>&1; then
            echo "formatting $DEVICE as xfs"
            mkfs.xfs -f -L techsara-data "$DEVICE"
          fi

          UUID="$(blkid -s UUID -o value "$DEVICE")"
          if ! grep -q "$UUID" /etc/fstab; then
            # nofail keeps a missing volume from blocking boot entirely.
            echo "UUID=$UUID $DATA_ROOT xfs defaults,noatime,nofail 0 2" >> /etc/fstab
          fi
          mount -a

          mkdir -p "$DATA_ROOT"/{postgres,backups,secrets,caddy/data,caddy/config,pgadmin}
          chmod 0700 "$DATA_ROOT/secrets"
          chown -R root:root "$DATA_ROOT/secrets"
          # The postgres container runs as uid 999 in the official image.
          chown -R 999:999 "$DATA_ROOT/postgres"
          echo "data volume ready at $DATA_ROOT"

      - path: /etc/docker/daemon.json
        content: |
          {
            "log-driver": "json-file",
            "log-opts": { "max-size": "10m", "max-file": "5" },
            "live-restore": true,
            "userland-proxy": false,
            "no-new-privileges": true
          }

      - path: /etc/sysctl.d/60-techsara.conf
        content: |
          # PostgreSQL and a busy API benefit from a larger connection backlog.
          net.core.somaxconn = 4096
          net.ipv4.tcp_max_syn_backlog = 4096
          # Overcommit tuned so the kernel does not kill PostgreSQL first.
          vm.overcommit_memory = 2
          vm.overcommit_ratio = 90
          vm.swappiness = 10

    runcmd:
      # --- Docker Engine from the official repository ---------------------
      - install -m 0755 -d /etc/apt/keyrings
      - curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
      - chmod a+r /etc/apt/keyrings/docker.asc
      - |
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" > /etc/apt/sources.list.d/docker.list
      - apt-get update
      - apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
      - systemctl enable --now docker

      # --- SSM Agent (preinstalled on Ubuntu images via snap) -------------
      - snap install amazon-ssm-agent --classic || true
      - systemctl enable --now snap.amazon-ssm-agent.amazon-ssm-agent.service || systemctl enable --now amazon-ssm-agent || true

      # --- AWS CLI v2 -----------------------------------------------------
      - |
        ARCH="$(uname -m)"
        curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-$ARCH.zip" -o /tmp/awscliv2.zip
        unzip -q -o /tmp/awscliv2.zip -d /tmp
        /tmp/aws/install --update
        rm -rf /tmp/aws /tmp/awscliv2.zip

      # --- Data volume ----------------------------------------------------
      - /usr/local/sbin/mount-data-volume.sh
      - sysctl --system

      # --- Application directory -----------------------------------------
      - mkdir -p /opt/${var.project_name}
      - chmod 0750 /opt/${var.project_name}

      - |
        cat > /etc/systemd/system/techsara-mount-data.service <<'UNIT'
        [Unit]
        Description=Mount the TechSara archive data volume
        DefaultDependencies=no
        Before=docker.service
        After=local-fs.target

        [Service]
        Type=oneshot
        RemainAfterExit=yes
        ExecStart=/usr/local/sbin/mount-data-volume.sh

        [Install]
        WantedBy=multi-user.target
        UNIT
      - systemctl daemon-reload
      - systemctl enable techsara-mount-data.service

      - echo "cloud-init finished; deploy the application with scripts/deploy_ec2.sh"

    final_message: "TechSara archive host ready after $UPTIME seconds"
  CLOUDINIT
}
