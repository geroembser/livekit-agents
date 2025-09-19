@Gero When you are updating livekit (by pulling from the livekit github repo) and you run into an lfs problem: 
- Change git config lfs.url to livekit by cd-ing into the livekit subdirectory and then running this command `git config --add lfs.url git@github.com:livekit/agents.git``
- Then update by pulling the main branch into your local branch. 
- Then before pushing, change the lfs.url back to the original by running `git config --add lfs.url git@github.com:geroembser/livekit-agents.git`
