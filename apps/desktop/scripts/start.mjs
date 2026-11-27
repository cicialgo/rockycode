import './check-dependencies.mjs';
import {spawn} from 'node:child_process';
import {electronPath} from './electron-path.mjs';
const env={...process.env}; delete env.ELECTRON_RUN_AS_NODE;
const child=spawn(electronPath(),['.'],{stdio:'inherit',env});
child.on('error',error=>{console.error(error.message);process.exitCode=1;});
child.on('exit',code=>process.exit(code??1));
