import { Composition, Folder } from "remotion";
import { ReelPedidoWhatsApp, duracionTotal } from "./ReelPedidoWhatsApp";

export const RemotionRoot: React.FC = () => {
  return (
    <Folder name="Instagram">
      <Composition
        id="ReelPedidoWhatsApp"
        component={ReelPedidoWhatsApp}
        durationInFrames={duracionTotal(30)}
        fps={30}
        width={1080}
        height={1920}
        defaultProps={{
          marca: "3B Technology",
          restaurante: "Edy Gourmet",
          cta: "Escribe DEMO a nuestro WhatsApp",
        }}
      />
    </Folder>
  );
};
