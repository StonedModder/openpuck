try:
    from .app import main
except ImportError:
    from openpuck_flasher.app import main

if __name__ == "__main__":
    main()
