import { loadFont } from "@remotion/fonts";
import { staticFile } from "remotion";

// Inter va dentro del proyecto (public/fonts) para que el render no dependa de internet.
const PESOS = { Regular: "400", SemiBold: "600", ExtraBold: "800", Black: "900" };
for (const [nombre, weight] of Object.entries(PESOS)) {
  loadFont({
    family: "Inter",
    url: staticFile(`fonts/Inter-${nombre}.otf`),
    weight,
    format: "opentype",
  });
}

export const fontFamily = `Inter, "Noto Color Emoji", sans-serif`;

export const colors = {
  fondo: "#0B141A",
  verde: "#25D366",
  verdeOscuro: "#128C7E",
  burbujaBot: "#005C4B",
  burbujaCliente: "#202C33",
  texto: "#E9EDEF",
  gris: "#8696A0",
  amarillo: "#FFD23F",
  rojo: "#FF3B30",
};
