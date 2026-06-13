import "@fontsource/inter/400.css";
import "@fontsource/inter/500.css";
import "@fontsource/inter/700.css";
import "@fontsource/jetbrains-mono/400.css";
import "@fontsource/jetbrains-mono/700.css";
import "remixicon/fonts/remixicon.css";
import "./lib/theme.css";
import App from "./App.svelte";

const app = new App({ target: document.getElementById("app")! });
export default app;
