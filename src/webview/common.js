(() => {
	const root = document.body;
	const vscode = acquireVsCodeApi();
	const chip = document.getElementById('stChip');
	const label = document.getElementById('stLabel');
	const refresh = document.getElementById('refresh');
	const restart = document.getElementById('restart');
	const language = document.getElementById('lang');
	if (!root || !chip || !label || !refresh) { return; }

	const text = (name) => decodeURIComponent(root.dataset[name] || '');
	const connectedText = text('connectedText');
	const disconnectedText = text('disconnectedText');
	const rebootLoaderLabel = text('rebootLoaderLabel');
	const rebootLoaderTip = text('rebootLoaderTip');
	const rebootEdlLabel = text('rebootEdlLabel');
	const rebootEdlTip = text('rebootEdlTip');

	function updateOpenRegionDensity() {
		const openCount = document.querySelectorAll('.region.open').length;
		root.classList.toggle('single-region-open', openCount === 1);
		root.classList.toggle('multiple-regions-open', openCount > 1);
	}

	function applyPlatform(platform) {
		const yocto = !!(platform && platform.sdk_type === 'yocto');
		document.querySelectorAll('[data-yocto-only]').forEach((el) => {
			el.style.display = yocto ? '' : 'none';
		});
		document.querySelectorAll('[data-yocto-hide]').forEach((el) => {
			el.style.display = yocto ? 'none' : '';
		});
		const tile = document.querySelector('[data-tile="qpiSdk.rebootLoader"]');
		if (!tile) { return; }
		const qc = !!(platform && platform.family === 'qualcomm');
		tile.querySelector('.lb').textContent = qc ? rebootEdlLabel : rebootLoaderLabel;
		tile.title = qc ? rebootEdlTip : rebootLoaderTip;
	}

	document.querySelectorAll('.region-h').forEach((header) => {
		header.addEventListener('click', () => {
			header.parentElement.classList.toggle('open');
			updateOpenRegionDensity();
		});
	});

	document.querySelectorAll('.tile').forEach((tile) => {
		tile.addEventListener('click', (event) => {
			event.stopPropagation();
			vscode.postMessage({ type: 'tile', cmd: tile.dataset.cmd });
		});
	});

	chip.addEventListener('click', () => vscode.postMessage({ type: 'connect' }));
	refresh.addEventListener('click', () => vscode.postMessage({ type: 'ready' }));
	restart?.addEventListener('click', () => vscode.postMessage({ type: 'tile', cmd: 'qpiSdk.restartBackend' }));
	language?.addEventListener('click', () => vscode.postMessage({ type: 'tile', cmd: 'qpiSdk.toggleLanguage' }));

	window.addEventListener('message', (event) => {
		const message = event.data;
		if (message.type !== 'status') { return; }
		chip.classList.toggle('on', !!message.connected);
		label.textContent = message.connected
			? (message.display || connectedText)
			: (message.display || disconnectedText);
		applyPlatform(message.platform);
	});

	updateOpenRegionDensity();
	vscode.postMessage({ type: 'ready' });
})();
