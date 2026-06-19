#line 1 "C:\\Users\\jarch\\Documents\\openpuck\\openpuck\\OpenPuck\\bonds.cpp"
#include "bonds.h"
#include <Adafruit_LittleFS.h>
#include <InternalFileSystem.h>
using namespace Adafruit_LittleFS_Namespace;

Slot g_slot[NSLOT];
int g_connSlot = -1;
unsigned long g_connReplyMs = 0;
volatile bool g_dirty = false;
bool g_pairing = false;
uint8_t g_pairingSlot = 0;
uint8_t g_pairingState = PAIR_IDLE;
uint8_t g_pairingChannel = 0x3C;
uint8_t g_pairingRecord[24] = { 0 };
unsigned long g_pairingSinceMs = 0;

#define BOND_FILE "/bonds.bin"

bool recEmpty(const uint8_t *r)
{
	for (int i = 0; i < 24; i++)
		if (r[i])
			return false;
	return true;
}

void saveBonds()
{
	InternalFS.remove(BOND_FILE);
	File f(InternalFS);
	if (f.open(BOND_FILE, FILE_O_WRITE)) {
		for (int i = 0; i < NSLOT; i++) {
			uint8_t u = g_slot[i].used ? 1 : 0;
			f.write(&u, 1);
			f.write(g_slot[i].rec, 24);
		}
		f.close();
	}
}

void loadBonds()
{
	File f(InternalFS);
	if (f.open(BOND_FILE, FILE_O_READ))
		for (int i = 0; i < NSLOT; i++) {
			uint8_t u = 0;
			if (f.read(&u, 1) == 1) {
				g_slot[i].used = u;
				f.read(g_slot[i].rec, 24);
			}
		}
	f.close();
}
