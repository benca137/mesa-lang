.PHONY: help mesa mesa-build mesa-rebuild

help:
	@printf '%s\n' \
		'Available targets:' \
		'  make mesa         Build and install the Mesa compiler binary' \
		'  make mesa-build   Build the Mesa compiler binary into ./bin' \
		'  make mesa-rebuild Rebuild and install the Mesa compiler binary'

mesa:
	@bash ./scripts/build-compiler.sh --install

mesa-build:
	@bash ./scripts/build-compiler.sh

mesa-rebuild:
	@bash ./scripts/rebuild-compiler.sh

lsp:
	cp -R /Users/oppenheimer/mesa_MVP/mesa2/src /Users/oppenheimer/.vscode/extensions/local.mesa-vscode/
	cp -R /Users/oppenheimer/mesa_MVP/mesa2/vscode/. /Users/oppenheimer/.vscode/extensions/local.mesa-vscode/