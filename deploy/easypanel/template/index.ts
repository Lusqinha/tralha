import { Output, Services } from "~templates-utils";
import { Input } from "./meta";

export function generate(input: Input): Output {
  const services: Services = [];
  const filesDomain = "files-$(EASYPANEL_DOMAIN)";

  const env = [
    "TRALHA_API_URL=https://$(EASYPANEL_DOMAIN)",
    `TRALHA_PUBLIC_URL=https://${filesDomain}`,
    "TRALHA_FORWARDED_ALLOW_IPS=10.0.0.0/8,172.16.0.0/12,192.168.0.0/16",
    `TRALHA_MAX_UPLOAD_MB=${input.maxUploadMb}`,
  ];
  if (input.adminAllowedIps) {
    env.push(`TRALHA_ADMIN_ALLOWED_IPS=${input.adminAllowedIps}`);
  }

  services.push({
    type: "app",
    data: {
      serviceName: input.appServiceName,
      source: {
        type: "git",
        repo: "https://github.com/Lusqinha/tralha.git",
        ref: input.gitRef,
        path: "/",
      },
      build: {
        type: "dockerfile",
        file: "Dockerfile",
      },
      env: env.join("\n"),
      domains: [
        { host: "$(EASYPANEL_DOMAIN)", port: 8000 },
        { host: filesDomain, port: 8001 },
      ],
      mounts: [{ type: "volume", name: "data", mountPath: "/data" }],
      deploy: {
        zeroDowntime: false,
        capDrop: ["ALL"],
      },
    },
  });

  return { services };
}
