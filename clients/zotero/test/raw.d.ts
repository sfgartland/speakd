// Vite's `?raw` imports: a file's text, as a string.
declare module "*?raw" {
  const content: string;
  export default content;
}
