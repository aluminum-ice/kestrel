PACKER_VERSION := 1.9.5
MY_HOSTNAME := kestrel
MY_VERSION := $(or ${MY_VERSION},$(shell cut -d"'" -f2 < _version.py))
# MY_VERSION := 1.0.0
MY_RELEASE := kestrel-$(MY_VERSION)

MACHINE_TYPE := $(shell uname -m)
ifneq (,$(filter x86_64,$(MACHINE_TYPE)))
GOARCH := amd64
else ifneq (,$(filter i686,$(MACHINE_TYPE)))
GOARCH := 386
else ifneq (,$(filter arm64% aarch64%,$(MACHINE_TYPE)))
GOARCH := arm64
else ifneq (,$(filter arm%,$(MACHINE_TYPE)))
GOARCH := arm
else
GOARCH := amd64
$(warning Unable to detect CPU arch from machine type $(MACHINE_TYPE), assuming $(GOARCH))
endif

# The Ansible part of the build can inadvertently change the active hostname of
# the build machine while updating the permanent hostname of the build image.
# If the unshare command is available, use it to create a separate namespace
# so hostname changes won't affect the build machine.
UNSHARE := $(shell command -v unshare)
ifneq (,$(UNSHARE))
UNSHARE := $(UNSHARE) --uts
endif

all: clean plugin image

PACKER := /tmp/raspberrypi/packer
PACKER_URL := https://releases.hashicorp.com/packer/$(PACKER_VERSION)/packer_$(PACKER_VERSION)_linux_$(GOARCH).zip
$(PACKER):
	mkdir -p $(@D)
	curl -L "$(PACKER_URL)" -o $(PACKER).zip
	unzip $(PACKER).zip -d $(@D)
	rm $(PACKER).zip
	chmod +x $@

# The solo-io/arm-image plugin is built from a pinned source commit because its
# newest published release (v0.2.7) has a race that sometimes finds only some
# loop partitions ("error different of partitions than expected"). The fix
# (v0.2.8) is tagged but has no release binaries. Needs Go on the build host;
# run `make plugin` as a normal user before `sudo make image` so root needs no Go.
ARM_IMAGE_TAG := v0.2.8
ARM_IMAGE_COMMIT := e1ace42451b208497fa4cc5c26084408a1f1269a
ARM_IMAGE_MODULE := github.com/solo-io/packer-plugin-arm-image
PACKER_PLUGIN_PATH := /tmp/raspberrypi/plugins
ARM_IMAGE_PLUGIN := $(PACKER_PLUGIN_PATH)/packer-plugin-arm-image
$(ARM_IMAGE_PLUGIN):
	rm -rf $(@D)/src
	git -c advice.detachedHead=false clone --quiet --depth 1 --branch $(ARM_IMAGE_TAG) https://$(ARM_IMAGE_MODULE).git $(@D)/src
	test "$$(git -C $(@D)/src rev-parse HEAD)" = "$(ARM_IMAGE_COMMIT)"
	cd $(@D)/src && go build -o $@ \
	  -ldflags "-X $(ARM_IMAGE_MODULE)/version.Version=$(ARM_IMAGE_TAG:v%=%) -X $(ARM_IMAGE_MODULE)/version.VersionPrerelease=" .
	rm -rf $(@D)/src

.PHONY: plugin
plugin: $(ARM_IMAGE_PLUGIN)

# PiShrink runs as root on the build host, so it is pinned to a release
# commit (v26.03.16) and checksum-verified instead of fetched from master.
# It runs with -s: the image's own first-boot hook grows the root filesystem
# (the playbook asserts the hook is present), and PiShrink's rc.local
# autoexpand would only add a reboot.
PISHRINK_COMMIT := a5f9463c01607ab07402c7e75c9cfd4bb3a0e886
PISHRINK_SHA256 := 71026f0c02ac099e588a3eb8f70760c1b680aa8ea3acde61a0141fbaeb68c777
PISHRINK := /tmp/raspberrypi/pishrink.sh
$(PISHRINK):
	mkdir -p $(@D)
	curl -fsSL "https://raw.githubusercontent.com/Drewsif/PiShrink/$(PISHRINK_COMMIT)/pishrink.sh" -o $@.tmp
	echo "$(PISHRINK_SHA256)  $@.tmp" | sha256sum -c -
	chmod +x $@.tmp
	mv $@.tmp $@

# Building the image requires these tools, but don't rebuild the image just because they updated.
$(MY_RELEASE).img: | $(PACKER) $(ARM_IMAGE_PLUGIN) $(PISHRINK)

# Packer's download cache (the base .img.xz). Packer verifies iso_checksum on
# reuse, so a stale entry is re-downloaded, never trusted. CI points this at a
# persistent directory on the build host.
PACKER_CACHE_DIR ?= $(CURDIR)/builder/packer_cache

EXTRA_DIRS=$(shell find builder/extra/ -type d)
EXTRA_FILES=$(shell find builder/extra/ -type f -name '*')

# If the packer or ansible files are updated, rebuild the image.
$(MY_RELEASE).img: builder/config.json builder/config.yml $(EXTRA_DIRS) $(EXTRA_FILES)
	cd builder && sudo env PACKER_PLUGIN_PATH=$(PACKER_PLUGIN_PATH) PACKER_CACHE_DIR=$(PACKER_CACHE_DIR) $(UNSHARE) $(PACKER) build -var "my_hostname=$(MY_HOSTNAME)" -var "my_version=$(MY_VERSION)" config.json
	sudo chown -R $$USER:$$USER builder/output-my_rpi_image
	mv builder/output-my_rpi_image/image $@
	$(PISHRINK) -s $@

# If any of these files are updated, rebuild the checksums.
$(MY_RELEASE).sha256: $(MY_RELEASE).img
	sha256sum $^ > $@

# If any of the input files are updated, rebuild the archive.
$(MY_RELEASE).7z: $(MY_RELEASE).img $(MY_RELEASE).sha256 
	7z a $(MY_RELEASE).7z $^

# Checksum of the archive itself, so a download can be verified before extracting.
$(MY_RELEASE).7z.sha256: $(MY_RELEASE).7z
	sha256sum $< > $@

.PHONY: image
image: $(MY_RELEASE).7z $(MY_RELEASE).7z.sha256

# Static checks only; does not build the image. Tools: requirements-lint.txt
.PHONY: lint
lint:
	ANSIBLE_DEPRECATION_WARNINGS=False ansible-playbook --syntax-check -i localhost, builder/config.yml < /dev/null
	yamllint --no-warnings .
	find . -name '*.sh' -not -path './.git/*' -print0 | xargs -0 shellcheck --severity=error
	# Byte-compile the Phase 3 Python package and plugins. PYTHONPYCACHEPREFIX
	# redirects the bytecode cache out of the tree so nothing is left under
	# builder/extra/ (every file there is a build input).
	tmp=$$(mktemp -d); \
	  PYTHONPYCACHEPREFIX=$$tmp find builder/extra/files -name '*.py' -print0 \
	  | xargs -0 python3 -m py_compile; status=$$?; rm -rf $$tmp; exit $$status
	# Nothing real in the repo: placeholder MACs/emails only, no keys, tokens,
	# captures or device data (CLAUDE.md, "Nothing real in the repo").
	python3 tools/real-data-check/real_data_check.py

# Framework unit tests (run on the build host or in CI, not on the device; need pytest and
# flask/requests). Not part of `lint`/CI, which only has the lint tools.
.PHONY: test
test:
	cd builder/extra/files/kestrel_pi && python3 -m pytest -q
	python3 -m pytest -q tools/real-data-check

clean:
	- rm -f $(PACKER) $(PISHRINK)
	- rm -rf $(PACKER_PLUGIN_PATH)
	- rm -f $(MY_RELEASE).*
	- sudo rm -rf builder/output-my_rpi_image builder/packer_cache


