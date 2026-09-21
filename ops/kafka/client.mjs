// Operator-only Kafka config. Application Pods receive their own identities.
import {fileURLToPath} from 'node:url';
import {existsSync} from 'node:fs';
const dir=fileURLToPath(new URL('../../secrets/kafka/pki/',import.meta.url));
export const secureBrokers=['10.4.4.2:9094','10.4.4.8:9094','10.4.4.5:9094'];
export function operatorTls(){
  const paths={'ssl.ca.location':dir+'ca.crt','ssl.certificate.location':dir+'operator.crt','ssl.key.location':dir+'operator.key'};
  for(const path of Object.values(paths))if(!existsSync(path))throw Error('Kafka operator TLS identity missing; restore protected PKI');
  return {'security.protocol':'ssl',...paths,'ssl.endpoint.identification.algorithm':'https','enable.ssl.certificate.verification':true};
}
