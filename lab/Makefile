# NetTwin lab. Run inside the WSL distro, or from Windows via `nettwin lab <target>` /
# scripts\wsl.ps1 <target>. The lab is deployed from a synced copy in $(LAB_RUN) so no
# container ever bind-mounts a /mnt/c path.
SHELL := /bin/bash
LAB ?= nettwin
LAB_RUN ?= $(HOME)/.nettwin/lab-run
CLAB ?= sudo containerlab
DOCKER ?= docker
FRR_VERSION ?= 10.2.1
ROUTERS := r1 r2 r3 r4 isp
HOSTS := sw1 h10 h20 srv inet
NODES := $(ROUTERS) $(HOSTS)
HERE := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))

.PHONY: help images sync deploy up down golden converge check shell serve stop-serve test-lab

help:
	@echo "targets:"
	@echo "  up        build images, sync, deploy, wait for convergence, run check"
	@echo "  down      destroy the lab"
	@echo "  golden    re-apply golden FRR config and kernel state on every node"
	@echo "  check     golden reachability check from the hosts"
	@echo "  converge  wait for OSPF Full and BGP Established"
	@echo "  images    build nettwin/frr:dev and nettwin/host:dev"
	@echo "  sync      copy lab/ to $(LAB_RUN)"
	@echo "  shell NODE=r1"

images:
	$(DOCKER) build --build-arg FRR_VERSION=$(FRR_VERSION) -t nettwin/frr:dev -f $(HERE)/Dockerfile.frr $(HERE)
	$(DOCKER) build -t nettwin/host:dev -f $(HERE)/Dockerfile.host $(HERE)

sync:
	mkdir -p $(LAB_RUN)/live
	rsync -a --delete --exclude live $(HERE)/ $(LAB_RUN)/
	rsync -a --delete $(HERE)/configs/ $(LAB_RUN)/live/
	chmod +x $(LAB_RUN)/scripts/*.sh $(LAB_RUN)/configs/*/setup.sh

deploy: sync
	cd $(LAB_RUN) && $(CLAB) deploy -t topology.clab.yml --reconfigure

up: images deploy converge check

down:
	cd $(LAB_RUN) && $(CLAB) destroy -t topology.clab.yml --cleanup

golden:
	@for n in $(ROUTERS); do \
	  echo "golden: $$n frr-reload"; \
	  $(DOCKER) exec clab-$(LAB)-$$n /usr/lib/frr/frr-reload.py --reload /golden/frr.conf >/dev/null || exit 1; \
	done
	@for n in $(NODES); do \
	  echo "golden: $$n setup.sh"; \
	  $(DOCKER) exec clab-$(LAB)-$$n sh /setup.sh || exit 1; \
	done
	@$(MAKE) --no-print-directory converge

converge:
	LAB=$(LAB) $(LAB_RUN)/scripts/converge.sh

check:
	LAB=$(LAB) $(LAB_RUN)/scripts/check.sh

shell:
	$(DOCKER) exec -it clab-$(LAB)-$(NODE) bash

serve stop-serve test-lab:
	@echo "$@: available from milestone M2"; exit 2
