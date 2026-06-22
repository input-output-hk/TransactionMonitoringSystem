// cytoscape-fcose ships no type declarations; it is only ever passed to
// `cytoscape.use(fcose)` as an extension, so an opaque default export suffices.
declare module "cytoscape-fcose" {
	const ext: cytoscape.Ext;
	export default ext;
}
