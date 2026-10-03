# GPU fleet health: single-node k3s bring-up and checks.
# Run targets in order: install-k3s -> install-operator -> install-monitoring.
SHELL := /bin/bash
.SHELLFLAGS := -euo pipefail -c

K3S_VERSION           ?=            # empty = latest stable, e.g. v1.33.4+k3s1
GPU_OPERATOR_VERSION  ?=            # empty = latest chart, e.g. v26.7.1
KPS_VERSION           ?=            # kube-prometheus-stack chart version
EXPECTED_GPUS         ?= 1
TEMP_LIMIT_C          ?= 83
export KUBECONFIG     ?= $(HOME)/.kube/config

helm_version = $(if $(strip $(1)),--version $(strip $(1)),)

.PHONY: help install-k3s install-operator install-monitoring health-check test

help:
	@grep -E '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-20s %s\n", $$1, $$2}'

install-k3s: ## Install k3s and copy the kubeconfig to ~/.kube/config
	curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION="$(strip $(K3S_VERSION))" sh -
	mkdir -p $(HOME)/.kube
	sudo install -m 600 -o $(shell id -u) -g $(shell id -g) /etc/rancher/k3s/k3s.yaml $(HOME)/.kube/config
	timeout 120 bash -c 'until kubectl get nodes 2>/dev/null | grep -q " Ready"; do sleep 2; done'

install-operator: ## Install the NVIDIA GPU Operator (host driver, toolkit on)
	helm repo add nvidia https://helm.ngc.nvidia.com/nvidia --force-update
	helm repo update nvidia
	kubectl create namespace gpu-operator --dry-run=client -o yaml | kubectl apply -f -
	kubectl apply -f k8s/gpu-operator/dcgm-metrics-configmap.yaml
	helm upgrade --install gpu-operator nvidia/gpu-operator -n gpu-operator \
	  $(call helm_version,$(GPU_OPERATOR_VERSION)) -f k8s/gpu-operator/values.yaml --wait --timeout 10m

install-monitoring: ## Install kube-prometheus-stack, dcgm ServiceMonitor, alerts, dashboard
	helm repo add prometheus-community https://prometheus-community.github.io/helm-charts --force-update
	helm repo update prometheus-community
	helm upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
	  -n monitoring --create-namespace $(call helm_version,$(KPS_VERSION)) \
	  -f k8s/monitoring/kube-prometheus-stack-values.yaml --wait --timeout 10m
	kubectl apply -f k8s/monitoring/dcgm-servicemonitor.yaml -f k8s/monitoring/alerts.yaml
	kubectl create configmap gpu-health-dashboard -n monitoring \
	  --from-file=k8s/monitoring/dashboards/gpu-health.json --dry-run=client -o yaml \
	  | kubectl label --local -f - grafana_dashboard=1 -o yaml \
	  | kubectl apply -f -

health-check: ## Run the local nvidia-smi health check (prints, never runs, cordon/drain)
	EXPECTED_GPUS=$(EXPECTED_GPUS) TEMP_LIMIT_C=$(TEMP_LIMIT_C) ./scripts/gpu-health-check.sh

test: ## Run fleet_check.py unit tests
	python3 -m unittest discover -s tests -v
