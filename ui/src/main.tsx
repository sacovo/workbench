import { createRoot } from "react-dom/client";
import { App } from "./App";
import { connect } from "./hub";
import "./styles.css";
import "./testhooks";

createRoot(document.getElementById("root")!).render(<App />);
connect();
